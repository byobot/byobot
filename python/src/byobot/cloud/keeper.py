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
from .crypto import CloudError, Identity, b64, dumps, new_key, proof_matches, unb64
from .folder import Exists, Folder, checksum

JOIN_TTL_S = 600
PLAN_BEHIND, PLAN_AHEAD = 1, 13

DDL = """
CREATE TABLE IF NOT EXISTS cloud_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_keys (epoch INTEGER PRIMARY KEY, key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_devices (
  id TEXT PRIMARY KEY, class TEXT NOT NULL, box TEXT NOT NULL, sign TEXT NOT NULL, joined_epoch INTEGER NOT NULL,
  moved_epoch INTEGER NOT NULL, last_seq INTEGER, removed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cloud_read (path TEXT PRIMARY KEY, checksum TEXT NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_shown (event_id TEXT PRIMARY KEY);
"""


class Keeper:
    def __init__(self, store: Store, folder: Folder, clock: Callable[[], float] = time.time):
        self.store, self.folder, self.clock = store, folder, clock
        for statement in DDL.strip().split(";"):
            if statement.strip():
                store.conn.execute(statement)
        if self._get("device_id") is None:
            raise ByobotError("the cloud logbook isn't set up for this store; run byobot cloud setup")
        self.device_id = self._get("device_id")
        self.identity = Identity.from_private(json.loads(self._get("identity")))

    # -- setup -----------------------------------------------------------------

    @classmethod
    def setup(cls, store: Store, folder: Folder, clock: Callable[[], float] = time.time) -> Keeper:
        """Make this store the folder's keeper and start the first epoch."""
        for statement in DDL.strip().split(";"):
            if statement.strip():
                store.conn.execute(statement)
        if store.conn.execute("SELECT 1 FROM cloud_meta WHERE key = 'device_id'").fetchone():
            raise ByobotError("this store is already the cloud logbook's keeper")
        if any(p.startswith("epochs/") for p in folder.list()):
            raise ByobotError("this cloud folder already has a keeper; restore with the recovery code instead")
        identity = Identity.generate()
        with store._tx():
            for key, value in (("device_id", uuid7()), ("identity", json.dumps(identity.private())),
                               ("epoch", "0"), ("seq", "1"), ("checkpoint_position", "0")):
                store.conn.execute("INSERT INTO cloud_meta VALUES (?, ?)", (key, value))
        keeper = cls(store, folder, clock)
        keeper._enrol({"id": keeper.device_id, "class": "keeper", **identity.public()}, 1)
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
            "SELECT * FROM cloud_devices WHERE removed = ? ORDER BY joined_epoch, id", (int(removed),))]

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
        events = self.store.events()
        position = self.store.position()
        parts = [events[i:i + rules.MAX_BATCH] for i in range(0, len(events), rules.MAX_BATCH)]
        with self.store._tx():
            self._set("epoch", n)
            for i, part in enumerate(parts):
                self._segment("checkpoint", i, [{"events": {"events": part}}])
            self._set("checkpoint_position", position)
            self.store.conn.execute("DELETE FROM cloud_shown")
            self._unset("plan_digest")
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
        apply(self.store, writer, device["class"], messages, self._shown)
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

    def publish_plan(self) -> bool:
        window = self.plan()
        digest = checksum(dumps(window))
        if self._get("plan_digest") == digest:
            return False
        old = [p for p in self.folder.list() if p.startswith("plan/")]
        with self.store._tx():
            path = self._segment("plan", self._next_seq(), [{"plan": window}])
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
        report = {"epoch": self.epoch, **self._admit(listing), **self._read(listing)}
        self._publish_device()
        report["published"] = self.publish_events()
        report["plan"] = self.publish_plan()
        return report


def apply(store: Store, writer: str, cls: str, messages: list[dict],
          shown: Callable[[list[str]], None] = lambda ids: None) -> list[tuple[str, str]]:
    """Accept a verified segment's messages from its writer: a relay's through the hub's rules, the keeper's as
    they are. One (outcome, reason) per event, in order."""
    out = []
    for m in messages:
        name, body = next(iter(m.items()))
        if name == "plan":
            continue
        if name == "samples":
            if cls == "relay":
                try:
                    rules.accept_samples(store, body)
                except ValidationFailed:
                    pass
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
