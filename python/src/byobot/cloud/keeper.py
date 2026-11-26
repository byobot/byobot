"""The keeper: the one device per store that holds the whole byobot logbook and looks after the cloud folder.

Each sync it admits a device that joined with the current join code, reads every segment it hasn't read
(a relay's through the same accept rules as the hub, its own in full), publishes what it holds that the folder
doesn't show yet, and publishes the plan when it changed. Its cloud state lives in the store, beside ``peers``.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlencode

from ..core import relay as rules
from ..core.store import Store
from ..util import ByobotError, ValidationFailed, uuid7
from . import files, segment
from .crypto import CloudError, Identity, b64, dumps, new_key, proof_matches, recovery_code, recovery_key, unb64
from .folder import Exists, Folder, checksum

JOIN_TTL_S = 600
MAX_FILES = 500  # past this, the keeper compacts into a new epoch
PLAN_BEHIND, PLAN_AHEAD = 1, 13

DDL = """
CREATE TABLE IF NOT EXISTS cloud_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_keys (epoch INTEGER PRIMARY KEY, key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_devices (
  id TEXT PRIMARY KEY, class TEXT NOT NULL, box TEXT NOT NULL, sign TEXT NOT NULL, joined_epoch INTEGER NOT NULL,
  moved_epoch INTEGER NOT NULL, last_seq INTEGER, removed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cloud_read (path TEXT PRIMARY KEY, checksum TEXT NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_shown (event_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS cloud_refused (path TEXT NOT NULL, line INTEGER NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY (path, line));
"""


class Keeper:
    def __init__(self, store: Store, folder: Folder, clock: Callable[[], float] = time.time):
        self.store, self.folder, self.clock = store, folder, clock
        _tables(store)
        if self._get("device_id") is None:
            raise ByobotError("the cloud logbook isn't set up for this store; run byobot cloud setup")
        self.device_id = self._get("device_id")
        self.identity = Identity.from_private(json.loads(self._get("identity")))
        self.recovery_code: str | None = None

    # -- setup -----------------------------------------------------------------

    @classmethod
    def setup(cls, store: Store, folder: Folder, clock: Callable[[], float] = time.time,
              recovery: bool = False) -> Keeper:
        """Make this store the folder's keeper and start the first epoch. With ``recovery``, ``recovery_code`` on
        the result is shown once and opens every later epoch's recovery key."""
        _tables(store)
        if store.conn.execute("SELECT 1 FROM cloud_meta WHERE key = 'device_id'").fetchone():
            raise ByobotError("this store is already the cloud logbook's keeper")
        if any(p.startswith("epochs/") for p in folder.list()):
            raise ByobotError("this cloud folder already has a keeper; restore with the recovery code instead")
        identity = Identity.generate()
        code = recovery_code() if recovery else None
        meta = [("device_id", uuid7()), ("identity", json.dumps(identity.private())), ("epoch", "0"), ("seq", "1"),
                ("checkpoint_position", "0")]
        if code:
            meta.append(("recovery_key", b64(recovery_key(code, store.store_id))))
        with store._tx():
            store.conn.executemany("INSERT INTO cloud_meta VALUES (?, ?)", meta)
            store.conn.execute("INSERT INTO cloud_devices (id, class, box, sign, joined_epoch, moved_epoch) VALUES "
                               "(?, 'keeper', ?, ?, 1, 1)", (meta[0][1], identity.public()["box"],
                                                             identity.public()["sign"]))
        keeper = cls(store, folder, clock)
        keeper.start_epoch()
        keeper.recovery_code = code
        return keeper

    @classmethod
    def recover(cls, store: Store, folder: Folder, code: str, clock: Callable[[], float] = time.time) -> Keeper:
        """Become the keeper again from the folder and the recovery code: on a new Mac with an empty store, or on
        one restored from an export of this store. Reads every epoch the code opens, then starts a new one."""
        _tables(store)
        if store.conn.execute("SELECT 1 FROM cloud_meta WHERE key = 'device_id'").fetchone():
            raise ByobotError("this store is already the cloud logbook's keeper")
        listing = folder.list()
        opened: dict[int, dict] = {}
        store_id, wrong = None, False
        for path in (p for p in listing if p.startswith("keys/") and p.endswith("/recovery.key")):
            data = folder.read(path) or b""
            try:
                found = json.loads(data)["store_id"]
                epoch = int(path.split("/")[1])
                opened[epoch] = files.open_recovery(data, recovery_key(code, found), found, epoch)
                store_id = found
            except (ValueError, KeyError, TypeError, CloudError) as exc:
                wrong = wrong or "recovery code" in str(exc)
        if wrong and not opened:
            raise ByobotError("that recovery code doesn't open this store")
        identity = epoch_body = None
        for n in sorted(opened, reverse=True):
            identity = Identity.from_private(opened[n]["keeper"])
            try:
                epoch_body = files.read_epoch(folder.read(files.epoch_path(n)) or b"", identity.public()["sign"],
                                              store_id, n)
                break
            except CloudError:
                continue
        if epoch_body is None:
            raise ByobotError("this cloud folder has no recovery key the code opens; restore from an export instead")
        top = opened[n]
        if store.conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
            if store.store_id != store_id or store.store_key.hex() != top["store_key"]:
                raise ByobotError("this store isn't the one the cloud folder belongs to")
        else:
            store.adopt(store_id, top["store_key"])
        own = [segment.parse_name(p) for p in listing if p.startswith((f"segments/{top['device_id']}/", "plan/"))]
        seq = max([w["seq"] for w in own if w] + [0]) + 1
        meta = [("device_id", top["device_id"]), ("identity", json.dumps(identity.private())), ("epoch", n),
                ("seq", seq), ("checkpoint_position", 0), ("recovery_key", b64(recovery_key(code, store_id)))]
        with store._tx():
            store.conn.executemany("INSERT INTO cloud_meta VALUES (?, ?)", [(k, str(v)) for k, v in meta])
            store.conn.executemany("INSERT INTO cloud_keys VALUES (?, ?)",
                                   [(e, c["epoch_key"]) for e, c in opened.items()])
            store.conn.executemany(
                "INSERT INTO cloud_devices (id, class, box, sign, joined_epoch, moved_epoch) VALUES (?, ?, ?, ?, ?, ?)",
                [(d["id"], d["class"], d["box"], d["sign"], n, n) for d in epoch_body["devices"]])
        keeper = cls(store, folder, clock)
        keeper._read(listing)
        keeper.start_epoch()
        return keeper

    # -- state -------------------------------------------------------------------

    def _get(self, key: str) -> str | None:
        row = self.store.conn.execute("SELECT value FROM cloud_meta WHERE key = ?", (key,)).fetchone()
        return row and row[0]

    def _set(self, key: str, value: Any) -> None:
        self.store.conn.execute("INSERT INTO cloud_meta VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                                "excluded.value", (key, str(value)))

    def _unset(self, key: str) -> None:
        self.store.conn.execute("DELETE FROM cloud_meta WHERE key = ?", (key,))

    @property
    def epoch(self) -> int:
        return int(self._get("epoch"))

    def key(self, epoch: int) -> bytes | None:
        row = self.store.conn.execute("SELECT key FROM cloud_keys WHERE epoch = ?", (epoch,)).fetchone()
        return row and unb64(row[0])

    def devices(self, removed: bool = False) -> list[dict]:
        return [dict(r) for r in self.store.conn.execute(
            "SELECT * FROM cloud_devices WHERE removed = ? ORDER BY class != 'keeper', joined_epoch, id", (int(removed),))]

    def device(self, device_id: str) -> dict | None:
        row = self.store.conn.execute("SELECT * FROM cloud_devices WHERE id = ?", (device_id,)).fetchone()
        return row and dict(row)

    def _enrol(self, device: dict, epoch: int) -> None:
        self.store.conn.execute("INSERT INTO cloud_devices (id, class, box, sign, joined_epoch, moved_epoch) "
                                "VALUES (?, ?, ?, ?, ?, ?)", (device["id"], device["class"], device["box"],
                                                              device["sign"], epoch, epoch))

    def _next_seq(self) -> int:
        seq = int(self._get("seq"))
        self._set("seq", seq + 1)
        return seq

    def _write(self, path: str, data: bytes) -> None:
        """Write a file once; a retry after a crash finds it there and replaces nothing."""
        try:
            self.folder.create(path, data)
        except Exists:
            if self.folder.read(path) != data:
                raise ByobotError(f"{path} is already in the cloud folder with other content") from None
        self.store.conn.execute("INSERT INTO cloud_read VALUES (?, ?, 'written') ON CONFLICT (path) DO UPDATE SET "
                                "checksum = excluded.checksum, outcome = excluded.outcome", (path, checksum(data)))

    def _segment(self, kind: str, seq: int, messages: list[dict]) -> str:
        path = segment.name(kind, self.epoch, seq, self.device_id)
        self._write(path, segment.write(kind, self.store.store_id, self.device_id, self.epoch, seq, messages,
                                        self.key(self.epoch), self.identity))
        return path

    # -- epochs ------------------------------------------------------------------

    def start_epoch(self) -> int:
        """Start the next epoch: a new key sealed to every active device, a checkpoint of the whole logbook, a
        plan, then the epoch file, which commits it."""
        n = self.epoch + 1
        key = new_key()
        store_id = self.store.store_id
        for path in self.folder.list():  # what a start cut short left behind; no device uses it yet
            if path.startswith((f"keys/{n:04d}/", f"checkpoints/{n:04d}/", f"plan/{n:04d}-")):
                self.folder.delete(path)
        with self.store._tx():
            self.store.conn.execute("INSERT INTO cloud_keys VALUES (?, ?) ON CONFLICT (epoch) DO UPDATE SET "
                                    "key = excluded.key", (n, b64(key)))
        devices = [{k: d[k] for k in ("id", "class", "box", "sign")} for d in self.devices()]
        for d in devices:
            if d["id"] != self.device_id:
                self._write(files.key_path(n, d["id"]), files.key_file(self.identity, store_id, n, d, key))
        if self._get("recovery_key"):
            content = {"epoch_key": b64(key), "store_key": self.store.store_key.hex(), "device_id": self.device_id,
                       "keeper": self.identity.private()}
            self._write(files.recovery_path(n), files.recovery_file(unb64(self._get("recovery_key")), store_id, n,
                                                                    content))
        with self.store._tx():  # the checkpoint and its position from one snapshot, so no event falls between
            events = self.store.events()
            position = self.store.position()
            parts = [events[i:i + rules.MAX_BATCH] for i in range(0, len(events), rules.MAX_BATCH)]
            self._set("epoch", n)
            for i, part in enumerate(parts):
                self._segment("checkpoint", i, [{"events": {"events": part}}])
            self._set("checkpoint_position", position)
            self.store.conn.execute("DELETE FROM cloud_shown")
            self._unset("plan_digest")
            self._unset("epoch_pending")
        self.publish_plan()
        self._write(files.epoch_path(n), files.epoch_file(self.identity, store_id, n, self.device_id, devices,
                                                          len(parts)))
        return n

    # -- joining -------------------------------------------------------------------

    def join_code(self) -> dict:
        """A one-time code a phone scans to join: valid ten minutes, for one device. It holds no key."""
        secret = secrets.token_bytes(16)
        expires = self.clock() + JOIN_TTL_S
        with self.store._tx():
            self._set("join", json.dumps({"secret": b64(secret), "expires": expires}))
        code = {"store_id": self.store.store_id, "secret": b64(secret), "keeper": self.identity.public()["sign"],
                "expires": expires}
        return {**code, "link": "byobot://cloud-join?" + urlencode(
            {"store": code["store_id"], "secret": code["secret"], "keeper": code["keeper"]})}

    def _admit(self, listing: dict[str, str]) -> dict:
        join = json.loads(self._get("join") or "null")
        if not join:
            return {}
        if self.clock() > join["expires"]:
            with self.store._tx():
                self._unset("join")
            return {"join": "expired"}
        secret = unb64(join["secret"])
        known = {d["id"] for d in self.devices()} | {d["id"] for d in self.devices(removed=True)}
        valid = []
        for path in listing:
            if not path.startswith("devices/") or not path.endswith(".json"):
                continue
            device_id = path[len("devices/"):-len(".json")]
            if device_id in known:
                continue
            data = self.folder.read(path)
            try:
                found = files.read_device(data or b"", self.store.store_id, device_id)
            except CloudError:
                continue
            public = {"box": found["box"], "sign": found["sign"]}
            if found["class"] == "relay" and proof_matches(secret, self.store.store_id, device_id, public,
                                                           found.get("proof")):
                valid.append(found)
        if not valid:
            return {}
        with self.store._tx():
            self._unset("join")
        if len(valid) > 1:
            return {"join": "refused: two devices answered one code; show a new code"}
        device = {k: valid[0][k] for k in ("id", "class", "box", "sign")}
        self._write(files.key_path(self.epoch, device["id"]),
                    files.key_file(self.identity, self.store.store_id, self.epoch, device, self.key(self.epoch)))
        with self.store._tx():
            self._enrol(device, self.epoch)
        return {"join": "admitted", "device": device["id"]}

    # -- reading -------------------------------------------------------------------

    def _read(self, listing: dict[str, str]) -> dict:
        report: dict[str, Any] = {"read": 0, "skipped": []}
        held = {r[0]: r[1] for r in self.store.conn.execute("SELECT path, checksum FROM cloud_read")}
        for path, sum_ in listing.items():
            where = segment.parse_name(path)
            if where is None or held.get(path) == sum_:
                continue
            outcome = self._read_one(path, where)
            with self.store._tx():
                self.store.conn.execute("INSERT INTO cloud_read VALUES (?, ?, ?) ON CONFLICT (path) DO UPDATE SET "
                                        "checksum = excluded.checksum, outcome = excluded.outcome",
                                        (path, sum_, outcome))
            if outcome == "read":
                report["read"] += 1
            elif outcome != "ignored":
                report["skipped"].append({"path": path, "reason": outcome})
                if where["kind"] == "segment" and where["device_id"] != self.device_id:
                    self.folder.delete(path)  # its writer sees it gone and sends what it held again
        return report

    def _read_one(self, path: str, where: dict) -> str:
        if where["kind"] == "plan":
            return "ignored"
        writer = self.device_id if where["kind"] == "checkpoint" else where["device_id"]
        device = self.device(writer)
        if device is None or device["removed"]:
            return "from a device that isn't in this logbook"
        key = self.key(where["epoch"])
        if key is None:
            return "from an epoch whose key this keeper no longer holds"
        data = self.folder.read(path)
        if data is None:
            return "ignored"
        try:
            _, messages = segment.read(data, path=path, store_id=self.store.store_id, key=key,
                                       sign_public=device["sign"])
        except CloudError as exc:
            return str(exc)
        refused: list[tuple[int, str]] = []
        apply(self.store, writer, device["class"], messages, self._shown, lambda i, why: refused.append((i, why)))
        with self.store._tx():
            self.store.conn.executemany("INSERT OR REPLACE INTO cloud_refused VALUES (?, ?, ?)",
                                        [(path, i, why) for i, why in refused])
        erased = set(self.store.erased())
        if erased and any(e.get("record", {}).get("kind") and (e["record"]["kind"], e["record"]["id"]) in erased
                          for m in messages for e in _events(m)):
            with self.store._tx():
                self._set("epoch_pending", "erase")  # it carried a record already erased here
        with self.store._tx():
            self.store.conn.execute("UPDATE cloud_devices SET moved_epoch = max(moved_epoch, ?), last_seq = "
                                    "max(coalesce(last_seq, 0), ?) WHERE id = ?",
                                    (where["epoch"], where["seq"], writer))
        return "read"

    def _shown(self, ids: list[str]) -> None:
        with self.store._tx():
            self.store.conn.executemany("INSERT OR IGNORE INTO cloud_shown VALUES (?)", [(i,) for i in ids])

    # -- writing -------------------------------------------------------------------

    def publish_events(self) -> int:
        """Publish every event this store holds that the folder doesn't show yet: its own, the source events it
        derived, and whatever a hub-paired phone pushed."""
        shown = {r[0] for r in self.store.conn.execute("SELECT event_id FROM cloud_shown")}
        events = [e for e in self.store.events(since=int(self._get("checkpoint_position")))
                  if e["id"] not in shown]
        for i in range(0, len(events), rules.MAX_BATCH):
            part = events[i:i + rules.MAX_BATCH]
            with self.store._tx():
                self._segment("segment", self._next_seq(), [{"events": {"events": part}}])
                self._shown([e["id"] for e in part])
        return len(events)

    def plan(self) -> dict:
        """The plan window every relay reads, with each relay's latest event held and refused events."""
        today = self.store.wall().date()
        start, end = today - timedelta(days=PLAN_BEHIND), today + timedelta(days=PLAN_AHEAD)
        window = rules.plan_window(self.store, start.isoformat(), end.isoformat())
        relays = [d["id"] for d in self.devices() if d["class"] == "relay"]
        window["received"] = {d: r for d in relays if (r := self.store.received(d))}
        window["rejected"] = [r for d in relays for r in self.store.rejections(d)]
        return window

    def receipts(self) -> dict:
        """Each relay's segments in this epoch that the keeper has read, by seq, and the samples it refused."""
        out: dict[str, dict] = {d["id"]: {"read": []} for d in self.devices() if d["class"] == "relay"}
        prefix = f"{self.epoch:04d}-"
        refused: dict[str, list] = {}
        for path, line, why in self.store.conn.execute("SELECT * FROM cloud_refused ORDER BY path, line"):
            refused.setdefault(path, []).append((line, why))
        for path, outcome in self.store.conn.execute("SELECT path, outcome FROM cloud_read WHERE path LIKE "
                                                     "'segments/%' ORDER BY path"):
            where = segment.parse_name(path)
            if where and where["device_id"] in out and path.rsplit("/", 1)[1].startswith(prefix) \
                    and outcome == "read":
                mine = out[where["device_id"]]
                mine["read"].append(where["seq"])
                for line, why in refused.get(path, []):
                    mine.setdefault("refused", []).append({"seq": where["seq"], "line": line, "reason": why})
        return out

    def publish_plan(self) -> bool:
        window = self.plan()
        messages = [{"plan": window}, {"receipts": self.receipts()}]
        digest = checksum(dumps(messages))
        if self._get("plan_digest") == digest:
            return False
        old = [p for p in self.folder.list() if p.startswith("plan/")]
        with self.store._tx():
            path = self._segment("plan", self._next_seq(), messages)
            self._set("plan_digest", digest)
        for p in old:
            if p != path:
                self.folder.delete(p)
        return True

    def _publish_device(self) -> None:
        path = files.device_path(self.device_id)
        if path not in self.folder.list():
            self._write(path, files.device_file(self.store.store_id, {"id": self.device_id, "class": "keeper",
                                                                      **self.identity.public()}))

    # -- sync ----------------------------------------------------------------------

    def sync(self) -> dict:
        listing = self.folder.list()
        report: dict[str, Any] = {}
        if files.epoch_path(self.epoch) not in listing:  # the folder was deleted, or a start was cut short
            report["reseeded"] = self.start_epoch()
            listing = self.folder.list()
        report.update(self._admit(listing))
        report.update(self._read(listing))
        report.update(self._cleanup())
        reason = self._new_epoch_reason(self.folder.list())
        if reason:
            report["new_epoch"] = {"epoch": self.start_epoch(), "reason": reason}
            report.update(self._cleanup())
        self._publish_device()
        report["published"] = self.publish_events()
        report["plan"] = self.publish_plan()
        return {"epoch": self.epoch, **report}

    def compact(self) -> int:
        """Rewrite the folder into a new epoch now; the old one is deleted once every device has moved."""
        return self.start_epoch()

    def remove(self, device_id: str) -> int:
        """Retire a device: a new epoch whose key isn't sealed to it. What it already read, it keeps."""
        device = self.device(device_id)
        if device is None or device["removed"]:
            raise ByobotError(f"no device {device_id} in this cloud logbook")
        if device["class"] == "keeper":
            raise ByobotError("the keeper can't remove itself")
        with self.store._tx():
            self.store.conn.execute("UPDATE cloud_devices SET removed = 1 WHERE id = ?", (device_id,))
        self.folder.delete(files.device_path(device_id))
        return self.start_epoch()

    def _new_epoch_reason(self, listing: dict[str, str]) -> str | None:
        last_erase = self.store.conn.execute("SELECT coalesce(max(seq), 0) FROM events WHERE op = 'erase'").fetchone()[0]
        if last_erase > int(self._get("checkpoint_position")):
            return "erase"
        if self._get("epoch_pending"):
            return self._get("epoch_pending")
        if sum(not p.startswith("checkpoints/") for p in listing) > MAX_FILES and not self._waiting():
            return "compact"
        return None

    def _waiting(self) -> list[str]:
        """Active devices that haven't written in the current epoch yet, so older epochs can't be deleted."""
        return [d["id"] for d in self.devices() if d["class"] == "relay" and d["moved_epoch"] < self.epoch]

    def _cleanup(self) -> dict:
        """Once every active device writes in the current epoch: delete every older file and key outright."""
        current = self.epoch
        old_keys = [r[0] for r in self.store.conn.execute("SELECT epoch FROM cloud_keys WHERE epoch < ?", (current,))]
        listing = self.folder.list()
        old = [p for p in listing if (e := _epoch_of(p)) is not None and e < current]
        if not old and not old_keys:
            return {}
        waiting = self._waiting()
        if waiting:
            return {"cleanup_waits_on": waiting}
        for path in old:
            self.folder.delete(path)
        with self.store._tx():
            self.store.conn.execute("DELETE FROM cloud_keys WHERE epoch < ?", (current,))
            self.store.conn.executemany("DELETE FROM cloud_read WHERE path = ?", [(p,) for p in old])
            self.store.conn.executemany("DELETE FROM cloud_refused WHERE path = ?", [(p,) for p in old])
        return {"cleaned": len(old)}

    def status(self) -> dict:
        listing = self.folder.list()
        return {"store_id": self.store.store_id, "epoch": self.epoch, "files": len(listing),
                "recovery": bool(self._get("recovery_key")),
                "devices": [{k: d[k] for k in ("id", "class", "joined_epoch", "moved_epoch", "last_seq")}
                            for d in self.devices()],
                "cleanup_waits_on": self._waiting() if any((e := _epoch_of(p)) is not None and e < self.epoch
                                                           for p in listing) else []}


def _tables(store: Store) -> None:
    for statement in DDL.strip().split(";"):
        if statement.strip():
            store.conn.execute(statement)


def _epoch_of(path: str) -> int | None:
    """The epoch a cloud file belongs to; None for a device file."""
    found = segment.parse_name(path)
    if found:
        return found["epoch"]
    parts = path.split("/")
    if parts[0] == "epochs" and len(parts) == 2 and parts[1][:4].isdigit():
        return int(parts[1][:4])
    if parts[0] == "keys" and len(parts) == 3 and parts[1].isdigit():
        return int(parts[1])
    return None


def _events(message: dict) -> list[dict]:
    body = message.get("events")
    events = body.get("events") if isinstance(body, dict) else None
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def apply(store: Store, writer: str, cls: str, messages: list[dict],
          shown: Callable[[list[str]], None] = lambda ids: None,
          refused: Callable[[int, str], None] = lambda line, why: None) -> list[tuple[str, str]]:
    """Accept a verified segment's messages from its writer: a relay's through the hub's rules, the keeper's as
    they are. One (outcome, reason) per event, in order; a refused samples message goes to ``refused``."""
    out = []
    for line, m in enumerate(messages):
        name, body = next(iter(m.items()))
        if name in ("plan", "receipts"):
            continue
        if name == "samples":
            if cls == "relay":
                try:
                    rules.accept_samples(store, body)
                except ValidationFailed as exc:
                    refused(line, str(exc))
            continue
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list):
            continue
        if cls == "relay":
            try:
                results = rules.push_events(store, writer, body)["results"]
            except ValidationFailed as exc:
                results = [{"status": "rejected", "reason": str(exc), "id": None} for _ in events]
            got = [(r["status"], r.get("reason", "")) for r in results]
        else:
            got = store.accept_many(events)
        out += got
        shown([e["id"] for e, (status, _) in zip(events, got) if status != "rejected" and isinstance(e, dict)])
    return out
