"""Schema 1's store: the byobot logbook (an append-only log of events), a record index over it and
per-record projections.

Each event writes one record. A record's heads are its put and retract events that no other event has seen
(through `prev`); more than one distinct head is a conflict, shown until a resolve names the heads it ends.
Accepting an event re-derives only that record, so cost doesn't grow with history.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import library
from ..util import ByobotError, Conflict, NotFound, ValidationFailed, uuid7
from . import hlc, mappers, occurrences, reducers
from .contract import SLUG_KINDS, Contract, Rejected, jcs, mac

EXPORT_VERSION = 1
STORE_VERSION = 5  # above every v0 store, so v0 refuses to open a v1 file
MAX_PREV = 64
COLUMNS = ("id", "kind", "record_id", "op", "kind_version", "hlc", "ts", "device_id", "actor_kind", "actor_id",
           "actor_model", "prev", "batch", "reverts", "redacted_by", "payload")
OPTIONAL = ("kind_version", "batch", "reverts", "redacted_by")

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, record_id TEXT NOT NULL,
  op TEXT NOT NULL, kind_version INTEGER, hlc TEXT NOT NULL, ts TEXT NOT NULL, device_id TEXT NOT NULL,
  actor_kind TEXT NOT NULL, actor_id TEXT NOT NULL, actor_model TEXT, prev TEXT NOT NULL, batch TEXT,
  reverts TEXT, redacted_by TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_record ON events (kind, record_id);
CREATE INDEX IF NOT EXISTS events_batch ON events (batch) WHERE batch IS NOT NULL;
DROP TRIGGER IF EXISTS events_append_only_update;
CREATE TRIGGER IF NOT EXISTS events_redact_only BEFORE UPDATE ON events
  WHEN OLD.op = 'erase' OR NEW.payload != '{}' OR NEW.redacted_by IS NULL OR NEW.seq IS NOT OLD.seq
    OR NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind OR NEW.record_id IS NOT OLD.record_id OR NEW.op IS NOT OLD.op
    OR NEW.kind_version IS NOT OLD.kind_version OR NEW.hlc IS NOT OLD.hlc OR NEW.ts IS NOT OLD.ts
    OR NEW.device_id IS NOT OLD.device_id OR NEW.actor_kind IS NOT OLD.actor_kind OR NEW.actor_id IS NOT OLD.actor_id
    OR NEW.actor_model IS NOT OLD.actor_model OR NEW.prev IS NOT OLD.prev OR NEW.batch IS NOT OLD.batch
    OR NEW.reverts IS NOT OLD.reverts
  BEGIN SELECT RAISE(ABORT, 'events are append-only; erase only blanks payloads'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS records (
  kind TEXT NOT NULL, id TEXT NOT NULL, current TEXT, live INTEGER NOT NULL DEFAULT 0,
  conflict INTEGER NOT NULL DEFAULT 0, erased INTEGER NOT NULL DEFAULT 0,
  heads TEXT NOT NULL DEFAULT '[]', tips TEXT NOT NULL DEFAULT '[]', PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS records_conflict ON records (conflict) WHERE conflict = 1;
CREATE TABLE IF NOT EXISTS unprojected (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, record_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clocks (device_id TEXT PRIMARY KEY, hlc TEXT NOT NULL, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peers (
  device_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, paired_at TEXT NOT NULL,
  last_seen_at TEXT, revoked_at TEXT);
"""
PEER_COLUMNS = "device_id, name, paired_at, last_seen_at, revoked_at"


def _local_now() -> datetime:
    return datetime.now().astimezone()


def stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _order(event: dict) -> tuple[str, str]:
    return event["hlc"], event["id"]


def _version(event: dict) -> str:
    return jcs([event["op"], event["payload"] if event["op"] == "put" else None])


class Store:
    def __init__(self, path: Path | str, *, contract: Contract | None = None, device_id: str | None = None,
                 wall: Callable[[], datetime] = _local_now, new_id: Callable[[], str] = uuid7):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.contract = contract or Contract.shipped()
        self.wall, self.new_id = wall, new_id
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # WAL's recommended level: no corruption on power loss, and no flush on every commit, which on
        # Windows costs more as the file grows.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        # Erase must leave nothing behind, so deleted and overwritten content is zeroed, not just unlinked.
        self.conn.execute("PRAGMA secure_delete=ON")
        self._migrate(device_id)
        self._compact()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _migrate(self, device_id: str | None) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, STORE_VERSION):
            self.conn.close()
            if version < STORE_VERSION:
                raise ByobotError(f"{self.path} was written by an older byobot that this one can't read; move it "
                                  "aside to start a new store, and import your sources again")
            raise ByobotError(f"{self.path} was written by a newer byobot (store version {version}); upgrade byobot")
        reshaped = version != 0 and self._get("shape") != reducers.SHAPE
        with self._tx():
            if reshaped:
                reducers.drop(self.conn)
            for ddl in (DDL, reducers.DDL):
                for statement in _statements(ddl):
                    self.conn.execute(statement)
            if version == 0:
                self._set("store_id", uuid7())
                self._set("store_key", secrets.token_hex(32))
                self._set("device_id", device_id or f"device-{secrets.token_hex(4)}")
                self._set("hlc", hlc.format((0, 0)))
                self._set("contract", self.contract.fingerprint())
                self.conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
            elif device_id is not None:
                self._set("device_id", device_id)
            self._set("shape", reducers.SHAPE)
            if self._get("library") != library.fingerprint():
                reducers.seed_library(self.conn)
                self._set("library", library.fingerprint())
        if reshaped:
            self.rebuild()
        self._repair_clock()
        if self._get("contract") != self.contract.fingerprint():
            self.use(self.contract)

    def _repair_clock(self) -> None:
        """A store that took a clock reading past the ceiling before such events were refused writes after the
        latest one that isn't."""
        if not hlc.out_of_range(hlc.parse(self._get("hlc"))):
            return
        held = [hlc.parse(r[0]) for r in self.conn.execute("SELECT hlc FROM events")]
        with self._tx():
            self._set("hlc", hlc.format(max((h for h in held if not hlc.out_of_range(h)), default=(0, 0))))

    def _tx(self):
        conn = self.conn

        class _Tx:
            def __enter__(self_inner):
                self_inner.owner = not conn.in_transaction
                conn.execute("BEGIN IMMEDIATE" if self_inner.owner else "SAVEPOINT tx")

            def __exit__(self_inner, exc_type, *_):
                if self_inner.owner:
                    conn.execute("ROLLBACK" if exc_type else "COMMIT")
                elif exc_type:
                    conn.execute("ROLLBACK TO tx")
                    conn.execute("RELEASE tx")
                else:
                    conn.execute("RELEASE tx")

        return _Tx()

    # -- meta ----------------------------------------------------------------

    def _get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row and row[0]

    def _set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                          (key, value))

    @property
    def store_id(self) -> str:
        return self._get("store_id")

    @property
    def device_id(self) -> str:
        return self._get("device_id")

    @property
    def store_key(self) -> bytes:
        return bytes.fromhex(self._get("store_key"))

    # -- the logbook ----------------------------------------------------------

    def event(self, event_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return row and _envelope(row)

    def events(self, since: int = 0, limit: int = -1) -> list[dict]:
        """Every event after arrival position `since`, in arrival order: at most `limit` (-1 for all)."""
        return [_envelope(r) for r in self.conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                                                        (since, limit))]

    def history(self, kind: str, rid: str) -> list[dict]:
        """One record's events, in arrival order."""
        return [_envelope(r) for r in self.conn.execute(
            "SELECT * FROM events WHERE kind = ? AND record_id = ? ORDER BY seq", (kind, rid))]

    def batch(self, batch_id: str) -> list[dict]:
        return [_envelope(r) for r in self.conn.execute("SELECT * FROM events WHERE batch = ? ORDER BY seq",
                                                        (batch_id,))]

    def position(self) -> int:
        return self.conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]

    def accept(self, event: Any) -> tuple[str, str]:
        """Take in one event from anywhere. Returns (outcome, reason); never raises for bad input."""
        return self.accept_many([event])[0]

    def accept_many(self, events: Iterable[Any]) -> list[tuple[str, str]]:
        out = []
        with self._tx():
            for event in events:
                with self._tx():
                    out.append(self._accept(event))
        self._compact()
        return out

    def _compact(self) -> None:
        """After an erase: rewrite the file and empty the WAL, so no page still holds what was erased. While
        another connection reads that can't finish, so it's tried again after each later write and on open."""
        if self.conn.in_transaction or not self.compact_pending():
            return
        self.conn.execute("PRAGMA busy_timeout=0")
        try:
            self.conn.execute("VACUUM")
            busy = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        except sqlite3.OperationalError:  # another connection is reading
            return
        finally:
            self.conn.execute("PRAGMA busy_timeout=5000")
        if not busy:
            self.conn.execute("DELETE FROM meta WHERE key = 'compact_pending'")

    def compact_pending(self) -> bool:
        """An erase whose content may still be on disk."""
        return self._get("compact_pending") is not None

    def _accept(self, event: Any) -> tuple[str, str]:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            return "rejected", "not an event"
        canonical = _canonical(event)
        if not canonical:
            return "rejected", "not JSON"
        held = self.event(event["id"])
        if held is not None:
            if "redacted_by" in held or "redacted_by" in event:
                same = _header(held) == _header(event)
            elif held["record"]["kind"] == "source_record":
                same = _content(held) == _content(event)
            else:
                same = jcs(held) == canonical
            return ("duplicate", "") if same else ("rejected", "id reused with other content")
        try:
            self.contract.check(event)
        except Rejected as exc:
            return "rejected", exc.reason
        if hlc.out_of_range(hlc.parse(event["hlc"])):
            return "rejected", "clock reading past 2200"
        if event["record"]["kind"] == "source_record" and event["op"] != "erase" and "redacted_by" not in event and \
                event["id"] != self._source_event_id(event):
            return "rejected", "broken rule: derived-id"
        kind, rid = event["record"]["kind"], event["record"]["id"]
        self._insert(event)
        erase = self._erase_of(kind, rid)
        if erase:
            self._redact(kind, rid, erase)
        understood = self.contract.understands(event)
        if not understood:
            self.conn.execute("INSERT INTO unprojected VALUES (?, ?, ?)",
                              (event["id"], event["record"]["kind"], event["record"]["id"]))
        self._reindex(event["record"]["kind"], event["record"]["id"])
        return ("accepted", "") if understood else ("stored_unprojected", "")

    def _erase_of(self, kind: str, rid: str) -> str | None:
        """The record's erase event, the earliest if several replicas erased it, so every replica names the same."""
        row = self.conn.execute("SELECT id FROM events WHERE kind = ? AND record_id = ? AND op = 'erase' "
                                "ORDER BY hlc, id LIMIT 1", (kind, rid)).fetchone()
        return row and row[0]

    def _redact(self, kind: str, rid: str, erase: str) -> None:
        """Blank every other event of an erased record, keeping ids and order so the logbook still replays."""
        changed = self.conn.execute(
            "UPDATE events SET payload = '{}', redacted_by = ? WHERE kind = ? AND record_id = ? AND op != 'erase' "
            "AND (redacted_by IS NULL OR redacted_by != ?)", (erase, kind, rid, erase)).rowcount
        if changed:
            self._set("compact_pending", erase)

    def _source_event_id(self, event: dict) -> str | None:
        """A source event's id: derived from its content with the store key, so two devices importing the same
        revision write one event, and nothing else can pass for it."""
        rid, prev = event["record"]["id"], event["prev"]
        if event["op"] == "put" and not prev:
            return self.contract.derived("source_revision", [rid, mac(self.store_key, event["payload"])])
        if event["op"] == "retract" and len(prev) == 1:
            return self.contract.derived("source_deletion", [rid, prev[0]])
        return None

    def _insert(self, e: dict) -> None:
        actor = e["actor"]
        self.conn.execute(f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})", (
            e["id"], e["record"]["kind"], e["record"]["id"], e["op"], e.get("kind_version"), e["hlc"], e["ts"],
            e["device_id"], actor["kind"], actor["id"], actor.get("model"), jcs(e["prev"]), e.get("batch"),
            e.get("reverts"), e.get("redacted_by"), jcs(e["payload"])))
        seen = hlc.parse(e["hlc"])
        floor = hlc.parse(self._get("hlc"))
        if seen > floor:
            self._set("hlc", e["hlc"])
        wall = self.wall()
        if hlc.runaway(seen, hlc.millis(wall)):
            self.conn.execute("INSERT INTO clocks VALUES (?, ?, ?) ON CONFLICT (device_id) DO UPDATE SET "
                              "hlc = max(hlc, excluded.hlc), seen_at = excluded.seen_at",
                              (e["device_id"], e["hlc"], stamp(wall)))

    # -- the record index ------------------------------------------------------

    def _reindex(self, kind: str, rid: str) -> None:
        rows = self.conn.execute("SELECT * FROM events WHERE kind = ? AND record_id = ?", (kind, rid)).fetchall()
        events = {r["id"]: _envelope(r) for r in rows}
        erased = any(e["op"] == "erase" for e in events.values())
        versions = {i: e for i, e in events.items() if e["op"] in ("put", "retract") and not erased}
        understood = {i: e for i, e in versions.items() if self.contract.understands(e)}
        if kind == "source_record":
            current, live, conflict, heads, tips = _source_current(understood)
        else:
            tips = sorted((e for i, e in versions.items() if i not in _ancestors(versions.values(), events)),
                          key=_order)
            heads = sorted((e for i, e in understood.items() if i not in _ancestors(understood.values(), events)),
                           key=_order)
            current = heads[-1] if heads else None
            live = current is not None and current["op"] == "put"
            conflict = len({_version(h) for h in heads}) > 1
        self.conn.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (kind, id) DO UPDATE SET "
            "current = excluded.current, live = excluded.live, conflict = excluded.conflict, "
            "erased = excluded.erased, heads = excluded.heads, tips = excluded.tips",
            (kind, rid, current and current["id"], int(live), int(conflict), int(erased),
             jcs([h["id"] for h in heads]), jcs([t["id"] for t in tips])))
        if self.contract.version(kind) is not None:
            self._project(kind, rid, current["payload"] if live else None)

    def _project(self, kind: str, rid: str, payload: dict | None) -> None:
        """Rewrite one record's rows, then what reads it: the source records it feeds and the merged workouts
        around it."""
        conn = self.conn
        touched = {(kind, rid)} | occurrences.links(conn, kind, rid)
        before = self._overlay_targets(kind, rid)
        spans = [s for k, i in touched | before if (s := occurrences.span(conn, k, i))]
        try:
            with self._tx():
                reducers.project(conn, self.contract, kind, rid, payload)
        except (KeyError, TypeError, ValueError, AttributeError):  # a newer version its reducer can't read
            reducers.project(conn, self.contract, kind, rid, None)
        after = self._overlay_targets(kind, rid)
        for k, i in sorted((before | after) - {(kind, rid)}):
            if k == "source_record" and (kind, rid) != (k, i):
                self._reproject(i)
        if kind == "source_record":
            for (dependent,) in conn.execute("SELECT id FROM source_links WHERE related_id = ?", (rid,)).fetchall():
                if dependent != rid:
                    self._reproject(dependent)
        touched |= before | after | occurrences.links(conn, kind, rid)
        spans += [s for k, i in touched if (s := occurrences.span(conn, k, i))]
        if kind in ("session", "source_record", "overlay"):
            occurrences.resolve(conn, touched, spans)
        elif kind == "setting" and rid == "source_priority":
            self._resolve_all()

    def _overlay_targets(self, kind: str, rid: str) -> set[tuple[str, str]]:
        if kind != "overlay":
            return set()
        row = self.conn.execute("SELECT target_kind, target_id, with_kind, with_id FROM overlays WHERE id = ?",
                                (rid,)).fetchone()
        if row is None:
            return set()
        return {(row[0], row[1])} | ({(row[2], row[3])} if row[3] else set())

    def _reproject(self, rid: str) -> None:
        found = self.record("source_record", rid)
        if found and found["live"]:
            self._project("source_record", rid, self.event(found["current"])["payload"])

    def _resolve_all(self) -> None:
        occurrences.clear(self.conn)
        everything = [(k, r[0]) for k, (table, _) in occurrences.ITEMS.items()
                      for r in self.conn.execute(f"SELECT id FROM {table}")]
        occurrences.resolve(self.conn, everything, [])

    def adopt(self, store_id: str, store_key: str) -> None:
        """Become a replica of another store: take its id and key. Only an empty store can, since events made with
        the old key no longer verify."""
        if self.conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
            raise Conflict("only an empty store can join another")
        with self._tx():
            self._set("store_id", store_id)
            self._set("store_key", store_key)

    def use(self, contract: Contract) -> None:
        """Switch to another contract (an upgrade or a downgrade) and re-derive every record it reads differently."""
        self.contract = contract
        with self._tx():
            unread = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT * FROM unprojected")}
            now = {}
            for row in self.conn.execute("SELECT id, kind, record_id, op, kind_version FROM events"):
                probe = {"record": {"kind": row[1], "id": row[2]}, "op": row[3], "kind_version": row[4]}
                if not contract.understands(probe):
                    now[row[0]] = (row[1], row[2])
            changed = {unread.get(i) or now.get(i) for i in set(unread) ^ set(now)}
            if self._units_changed(contract):
                changed |= set(self.conn.execute(
                    "SELECT kind, id FROM records WHERE live AND kind IN ('planned_session', 'session', 'effort', "
                    "'observation')").fetchall())
            self.conn.execute("DELETE FROM unprojected")
            self.conn.executemany("INSERT INTO unprojected VALUES (?, ?, ?)", [(i, *r) for i, r in now.items()])
            for kind, rid in sorted(changed):
                self._reindex(kind, rid)
            self._set("contract", contract.fingerprint())

    def _units_changed(self, contract: Contract) -> bool:
        try:
            return json.loads(self._get("contract") or "[]")[2:] != json.loads(contract.fingerprint())[2:]
        except ValueError:
            return True

    def rebuild(self) -> None:
        """Re-derive every record from the logbook. A repair tool: accepting never needs it."""
        with self._tx():
            self.conn.execute("DELETE FROM records")
            reducers.clear(self.conn)
            for kind, rid in self.conn.execute("SELECT DISTINCT kind, record_id FROM events").fetchall():
                self._reindex(kind, rid)
            self._resolve_all()

    # -- reads ---------------------------------------------------------------

    def record(self, kind: str, rid: str) -> dict | None:
        """The record's index row: current event, whether it's live, conflicted or erased, and its heads."""
        row = self.conn.execute("SELECT * FROM records WHERE kind = ? AND id = ?", (kind, rid)).fetchone()
        if row is None:
            return None
        return {"kind": kind, "id": rid, "current": row["current"], "live": bool(row["live"]),
                "conflict": bool(row["conflict"]), "erased": bool(row["erased"]),
                "heads": json.loads(row["heads"]), "tips": json.loads(row["tips"])}

    def get(self, kind: str, rid: str) -> dict | None:
        """The live record's content and who wrote it, or None."""
        found = self.record(kind, rid)
        if not found or not found["live"]:
            return None
        event = self.event(found["current"])
        return {"kind": kind, "id": rid, "payload": event["payload"], "event": event, "conflict": found["conflict"]}

    def records(self, kind: str | None = None) -> list[dict]:
        """Every live record, current version, in kind then id order."""
        sql = ("SELECT r.kind, r.id, r.conflict, e.* FROM records r JOIN events e ON e.id = r.current "
               "WHERE r.live = 1" + (" AND r.kind = ?" if kind else "") + " ORDER BY r.kind, r.id")
        return [{"kind": r[0], "id": r[1], "conflict": bool(r[2]), "payload": json.loads(r["payload"]),
                 "event": _envelope(r)} for r in self.conn.execute(sql, (kind,) if kind else ())]

    def conflicts(self, kind: str | None = None, rid: str | None = None) -> list[dict]:
        """Records with more than one current version, each version with its author, device and time."""
        sql = "SELECT kind, id, heads FROM records WHERE conflict = 1"
        args: list = []
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        if rid:
            sql, args = sql + " AND id = ?", args + [rid]
        out = []
        for k, i, heads in self.conn.execute(sql + " ORDER BY kind, id", args).fetchall():
            out.append({"kind": k, "id": i, "versions": [self.event(h) for h in json.loads(heads)]})
        return out

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params))]

    def erased(self) -> list[tuple[str, str]]:
        return [tuple(r) for r in self.conn.execute("SELECT kind, id FROM records WHERE erased = 1 ORDER BY kind, id")]

    def clock_warnings(self) -> list[dict]:
        """Devices whose clocks run far ahead of this one's."""
        return [dict(r) for r in self.conn.execute("SELECT * FROM clocks ORDER BY device_id")]

    def counts(self) -> dict:
        tables = ("events", "records", "activities", "locations", "plans", "planned_sessions", "sessions", "efforts",
                  "observations", "source_records", "overlays", "occurrences")
        out = {t: self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        return {**out, "conflicts": self.conn.execute("SELECT count(*) FROM records WHERE conflict").fetchone()[0]}

    # -- backup ----------------------------------------------------------------

    def export(self) -> Iterable[str]:
        """The whole logbook as JSON lines: a header with the store's id and key, then every event in arrival
        order. Paired devices stay behind; they pair again with the restored store."""
        yield jcs({"byobot_export": EXPORT_VERSION, "schema_version": 1, "store_id": self.store_id,
                   "store_key": self._get("store_key"), "exported_at": stamp(self.wall()),
                   "events": self.position()}) + "\n"
        for event in self.events():
            yield jcs(event) + "\n"

    def restore(self, lines: Iterable[str]) -> dict:
        """Become the store an export came from: its id, its key and every event. Only an empty store can."""
        it = iter(lines)
        try:
            header = json.loads(next(it))
        except (StopIteration, ValueError):
            raise ValidationFailed("not a byobot export") from None
        if not isinstance(header, dict) or header.get("byobot_export") != EXPORT_VERSION:
            raise ValidationFailed("not a byobot export this version can read")
        outcomes: dict[str, int] = {}
        with self._tx():  # all or nothing
            self.adopt(header["store_id"], header["store_key"])
            for line in it:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                outcome, _ = self._accept(event)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if sum(outcomes.values()) != header.get("events"):
                raise ValidationFailed(f"the export is cut short: {sum(outcomes.values())} of {header.get('events')} "
                                       "events")
        self._compact()
        return {"store_id": self.store_id, "events": outcomes}

    # -- paired devices: local to this store, never in the logbook -----------------

    def pair(self, device_id: str, name: str, token_sha256: str) -> dict:
        """Pair a device, or pair it again with a new token (which also lifts an earlier unpair)."""
        at = stamp(self.wall())
        with self._tx():
            self.conn.execute(
                "INSERT INTO peers VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT (device_id) DO UPDATE SET "
                "name = excluded.name, token_sha256 = excluded.token_sha256, paired_at = excluded.paired_at, "
                "last_seen_at = excluded.last_seen_at, revoked_at = NULL", (device_id, name, token_sha256, at, at))
        return self.peer(device_id)

    def peer(self, device_id: str) -> dict | None:
        row = self.conn.execute(f"SELECT {PEER_COLUMNS} FROM peers WHERE device_id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def peer_for_token(self, token_sha256: Any) -> dict | None:
        """The paired device a token belongs to, if it is still paired, marked as seen. Every live hash is compared
        in constant time, so the check doesn't stop at the first byte that differs or the first peer that fits."""
        offered = token_sha256 if isinstance(token_sha256, str) else ""
        found = None
        for device_id, stored in self.conn.execute(
                "SELECT device_id, token_sha256 FROM peers WHERE revoked_at IS NULL").fetchall():
            if len(stored) == len(offered) and secrets.compare_digest(stored, offered):
                found = device_id
        if found is None:
            return None
        with self._tx():
            self.conn.execute("UPDATE peers SET last_seen_at = ? WHERE device_id = ?", (stamp(self.wall()), found))
        return self.peer(found)

    def peers(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(f"SELECT {PEER_COLUMNS} FROM peers ORDER BY paired_at DESC")]

    def unpair(self, device_id: str) -> dict:
        if not self.peer(device_id):
            raise NotFound(f"no paired device {device_id}")
        with self._tx():
            self.conn.execute("UPDATE peers SET revoked_at = coalesce(revoked_at, ?) WHERE device_id = ?",
                              (stamp(self.wall()), device_id))
        return self.peer(device_id)

    # -- writing ---------------------------------------------------------------

    def write(self, actor: dict, changes: list[dict]) -> list[dict]:
        """Author events, all or none. Each change is {kind, id, op, payload?, reverts?, prev?}."""
        batch = self.new_id() if len(changes) > 1 else None
        written = []
        with self._tx():
            for change in changes:
                event = self._event(actor, change, batch)
                outcome, reason = self._accept(event)
                if outcome != "accepted":
                    raise ValidationFailed(f"{change['kind']} {change['id']}: {reason or outcome}")
                written.append(event)
        self._compact()
        return written

    def _event(self, actor: dict, change: dict, batch: str | None) -> dict:
        kind, rid, op = change["kind"], change["id"], change["op"]
        found = self.record(kind, rid)
        if found and kind != "source_record" and op != "erase" and not set(found["tips"]) <= set(found["heads"]):
            raise Conflict(f"{kind} {rid} holds a version written by a newer byobot; upgrade to change it")
        prev = change.get("prev")
        if prev is None:  # a write names the version it was made from, so a concurrent one stays a conflict
            prev = [found["current"]] if found and found["current"] else []
        if len(prev) > MAX_PREV:
            raise Conflict(f"{kind} {rid} has more than {MAX_PREV} current versions; resolve some first")
        wall = self.wall()
        reading = hlc.tick(hlc.parse(self._get("hlc")), hlc.millis(wall))
        event = {"id": change.get("event_id") or self.new_id(), "schema_version": 1, "record": {"kind": kind, "id": rid}, "op": op,
                 "hlc": hlc.format(reading), "ts": stamp(wall), "device_id": self.device_id,
                 "actor": {k: v for k, v in actor.items() if k in ("kind", "id", "model")},
                 "prev": prev, "payload": change.get("payload", {}) if op != "erase" else {}}
        if op == "put":
            event["kind_version"] = change.get("kind_version") or self.contract.version(kind)
        if change.get("reverts"):
            event["reverts"] = change["reverts"]
        if batch:
            event["batch"] = batch
        return event

    def new_record_id(self, kind: str) -> str:
        if kind in SLUG_KINDS:
            raise ValidationFailed(f"a {kind} needs an id")
        return self.new_id()


def ddl() -> str:
    """The store's SQL tables, for people and agents writing queries."""
    return "\n".join(s.strip() for s in _statements(DDL + reducers.DDL)) + "\n"


_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
# The store's own key and the paired devices' token hashes stay out of queries.
_UNREADABLE = {("meta", None), ("peers", "token_sha256")}


def _authorize(action: int, table: Any, column: Any, *_: Any) -> int:
    if action == sqlite3.SQLITE_READ and ((table, None) in _UNREADABLE or (table, column) in _UNREADABLE):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


def query(path: Path, sql: str, params: Iterable[Any] = (), limit: int = 1000, seconds: float = 10) -> dict:
    """Run one read-only SQL statement, stopped after `seconds`. Writes, ATTACH and PRAGMA are refused."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_authorize)
        deadline = time.monotonic() + seconds
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            cur = conn.execute(sql, tuple(params))
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(limit + 1)
        except (sqlite3.DatabaseError, sqlite3.Warning) as exc:
            if time.monotonic() > deadline:
                raise ValidationFailed(f"query stopped after {seconds:g} s; narrow it or add a LIMIT") from None
            raise ValidationFailed(f"query refused: {exc}") from None
        return {"columns": columns, "rows": [dict(zip(columns, r)) for r in fetched[:limit]],
                "truncated": len(fetched) > limit}
    finally:
        conn.close()


def _ancestors(starts: Iterable[dict], events: dict[str, dict]) -> set[str]:
    """Every event id reachable through `prev` from `starts`, held or not."""
    seen: set[str] = set()
    stack = [p for e in starts for p in e["prev"]]
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        if i in events:
            stack.extend(events[i]["prev"])
    return seen


def _source_current(versions: dict[str, dict]) -> tuple:
    """A source record's current revision: the latest by the source's own account, else the last written. A deletion
    hides the record while the revision it names is current. Source records never conflict."""
    puts = [e for e in versions.values() if e["op"] == "put"]
    current = max(puts, key=mappers.revision_order) if puts else None
    hidden = current is not None and any(e["op"] == "retract" and e["prev"] == [current["id"]]
                                         for e in versions.values())
    shown = [current] if current else []
    return current, current is not None and not hidden, False, shown, shown


def _header(event: dict) -> str:
    """An event without what erase blanks."""
    return jcs({k: v for k, v in event.items() if k not in ("payload", "redacted_by")})


def _content(event: dict) -> str:
    return jcs([event["record"], event["op"], event.get("kind_version"), event["prev"], event["payload"]])


def _envelope(row: sqlite3.Row) -> dict:
    e = {"id": row["id"], "schema_version": 1, "record": {"kind": row["kind"], "id": row["record_id"]},
         "op": row["op"], "hlc": row["hlc"], "ts": row["ts"], "device_id": row["device_id"],
         "actor": {"kind": row["actor_kind"], "id": row["actor_id"]}, "prev": json.loads(row["prev"]),
         "payload": json.loads(row["payload"])}
    if row["actor_model"] is not None:
        e["actor"]["model"] = row["actor_model"]
    for key in OPTIONAL:
        if row[key] is not None:
            e[key] = row[key]
    return e


def _canonical(event: dict) -> str:
    try:
        return jcs(event)
    except (TypeError, ValueError):
        return ""


def _statements(ddl: str) -> list[str]:
    out, pending = [], ""
    for line in ddl.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            out.append(pending)
            pending = ""
    return out
