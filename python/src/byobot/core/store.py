"""Schema 1's store: an append-only log of events, a record index over it and per-record projections.

Each event writes one record. A record's heads are its put and retract events that no other event has seen
(through `prev`); more than one distinct head is a conflict, shown until a write that names every head.
Accepting an event re-derives only that record, so cost doesn't grow with history.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..util import Conflict, ValidationFailed, uuid7
from . import hlc, reducers
from .contract import SLUG_KINDS, Contract, Rejected, jcs

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
CREATE TRIGGER IF NOT EXISTS events_append_only_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS records (
  kind TEXT NOT NULL, id TEXT NOT NULL, current TEXT, live INTEGER NOT NULL DEFAULT 0,
  conflict INTEGER NOT NULL DEFAULT 0, erased INTEGER NOT NULL DEFAULT 0,
  heads TEXT NOT NULL DEFAULT '[]', tips TEXT NOT NULL DEFAULT '[]', PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS records_conflict ON records (conflict) WHERE conflict = 1;
CREATE TABLE IF NOT EXISTS unprojected (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, record_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clocks (device_id TEXT PRIMARY KEY, hlc TEXT NOT NULL, seen_at TEXT NOT NULL);
"""


def _local_now() -> datetime:
    return datetime.now().astimezone()


def stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _order(event: dict) -> tuple[str, str]:
    return event["hlc"], event["id"]


def _version(event: dict) -> str:
    return jcs([event["op"], event.get("kind_version"), event["payload"] if event["op"] == "put" else None])


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
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate(device_id)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _migrate(self, device_id: str | None) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, STORE_VERSION):
            raise RuntimeError(f"{self.path} is store version {version}; this byobot reads {STORE_VERSION}")
        with self._tx():
            for ddl in (DDL, reducers.DDL):
                for statement in _statements(ddl):
                    self.conn.execute(statement)
            if version == 0:
                self._set("store_id", uuid7())
                self._set("store_key", secrets.token_hex(32))
                self._set("device_id", device_id or f"device-{secrets.token_hex(4)}")
                self._set("hlc", hlc.format((0, 0)))
                self.conn.execute(f"PRAGMA user_version = {STORE_VERSION}")
            elif device_id is not None:
                self._set("device_id", device_id)

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
    def device_id(self) -> str:
        return self._get("device_id")

    @property
    def store_key(self) -> bytes:
        return bytes.fromhex(self._get("store_key"))

    # -- the log -------------------------------------------------------------

    def event(self, event_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return row and _envelope(row)

    def events(self, since: int = 0) -> list[dict]:
        """Every event after arrival position `since`, in arrival order."""
        return [_envelope(r) for r in self.conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (since,))]

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
        return out

    def _accept(self, event: Any) -> tuple[str, str]:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            return "rejected", "not an event"
        held = self.event(event["id"])
        if held is not None:
            return ("duplicate", "") if jcs(held) == _canonical(event) else ("rejected", "id reused with other content")
        try:
            self.contract.check(event)
        except Rejected as exc:
            return "rejected", exc.reason
        self._insert(event)
        understood = self.contract.understands(event)
        if not understood:
            self.conn.execute("INSERT INTO unprojected VALUES (?, ?, ?)",
                              (event["id"], event["record"]["kind"], event["record"]["id"]))
        self._reindex(event["record"]["kind"], event["record"]["id"])
        return ("accepted", "") if understood else ("stored_unprojected", "")

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
        unknown = {r[0] for r in self.conn.execute(
            "SELECT event_id FROM unprojected WHERE kind = ? AND record_id = ?", (kind, rid))}
        erased = any(e["op"] == "erase" for e in events.values())
        versions = {i: e for i, e in events.items() if e["op"] in ("put", "retract") and not erased}
        understood = {i: e for i, e in versions.items() if i not in unknown}
        tips = sorted((e for i, e in versions.items() if i not in _ancestors(versions.values(), events)), key=_order)
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
            try:
                with self._tx():
                    reducers.project(self.conn, kind, rid, current["payload"] if live else None)
            except (KeyError, TypeError):  # a newer kind version this install's reducer can't read
                reducers.project(self.conn, kind, rid, None)

    def reproject(self) -> int:
        """Project events this install has since learned to read (after an upgrade). Returns how many."""
        learned = [r for r in self.conn.execute("SELECT u.event_id, u.kind, u.record_id, e.op, e.kind_version "
                                                "FROM unprojected u JOIN events e ON e.id = u.event_id")
                   if self.contract.understands({"record": {"kind": r[1], "id": r[2]}, "op": r[3],
                                                 "kind_version": r[4]})]
        with self._tx():
            for event_id, *_ in learned:
                self.conn.execute("DELETE FROM unprojected WHERE event_id = ?", (event_id,))
            for kind, rid in {(r[1], r[2]) for r in learned}:
                self._reindex(kind, rid)
        return len(learned)

    def rebuild(self) -> None:
        """Re-derive every record from the log. A repair tool: accepting never needs it."""
        with self._tx():
            self.conn.execute("DELETE FROM records")
            reducers.clear(self.conn)
            for kind, rid in self.conn.execute("SELECT DISTINCT kind, record_id FROM events").fetchall():
                self._reindex(kind, rid)

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

    def erased(self) -> list[tuple[str, str]]:
        return [tuple(r) for r in self.conn.execute("SELECT kind, id FROM records WHERE erased = 1 ORDER BY kind, id")]

    def clock_warnings(self) -> list[dict]:
        """Devices whose clocks run far ahead of this one's."""
        return [dict(r) for r in self.conn.execute("SELECT * FROM clocks ORDER BY device_id")]

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
        return written

    def _event(self, actor: dict, change: dict, batch: str | None) -> dict:
        kind, rid, op = change["kind"], change["id"], change["op"]
        found = self.record(kind, rid)
        prev = change.get("prev", found["tips"] if found else [])
        if len(prev) > MAX_PREV:
            raise Conflict(f"{kind} {rid} has more than {MAX_PREV} current versions; resolve some first")
        wall = self.wall()
        reading = hlc.tick(hlc.parse(self._get("hlc")), hlc.millis(wall))
        event = {"id": self.new_id(), "schema_version": 1, "record": {"kind": kind, "id": rid}, "op": op,
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
