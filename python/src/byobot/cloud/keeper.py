"""The keeper: the one device per store that holds the whole byobot logbook and looks after the cloud folder.

Each sync it admits a device that joined with the current join code, reads every segment it hasn't read
(a relay's through the same accept rules as the hub, its own in full), publishes what it holds that the folder
doesn't show yet, and publishes the plan when it changed. Its cloud state lives in the store, beside ``peers``.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

from .. import __version__
from ..core import relay as rules
from ..core import sources
from ..core.store import Store
from ..util import ByobotError, ValidationFailed, uuid7
from . import files, segment
from .crypto import CloudError, Identity, b64, dumps, new_key, proof_matches, recovery_code, recovery_key, unb64
from .folder import Exists, Folder, checksum
from .relay import is_epoch_file

JOIN_TTL_S = 600
MAX_FILES = 500  # past this, the keeper compacts into a new epoch
PLAN_BEHIND, PLAN_AHEAD = 1, 13
MISSING_S = 30  # past Drive's change-feed lag
SEQ_CEILING = 10**9  # a name past this wasn't the keeper's: it writes one seq at a time
NEWER = "newer byobot"  # in a skip reason: kept in the folder until this byobot is upgraded

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
            raise ByobotError("this cloud folder already has a keeper; run `byobot cloud recover` with the recovery code instead")
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
        devices = {d["id"]: d for d in epoch_body["devices"]}
        for path in listing:  # admitted since the epoch file was written: only a key file names them
            if path.startswith(f"keys/{n:04d}/") and not path.endswith("/recovery.key"):
                try:
                    d = files.key_device(folder.read(path) or b"", identity.public()["sign"], store_id, n)
                except CloudError:
                    continue
                devices.setdefault(d["id"], d)
        if store.conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
            if store.store_id != store_id or store.store_key.hex() != top["store_key"]:
                raise ByobotError("this store isn't the one the cloud folder belongs to")
        else:
            store.adopt(store_id, top["store_key"])
        meta = [("device_id", top["device_id"]), ("identity", json.dumps(identity.private())), ("epoch", n),
                ("seq", _top_seq(listing, top["device_id"])), ("checkpoint_position", 0),
                ("recovery_key", b64(recovery_key(code, store_id)))]
        with store._tx():
            store.conn.executemany("INSERT INTO cloud_meta VALUES (?, ?)", [(k, str(v)) for k, v in meta])
            store.conn.executemany("INSERT INTO cloud_keys VALUES (?, ?)",
                                   [(e, c["epoch_key"]) for e, c in opened.items()])
            store.conn.executemany(
                "INSERT INTO cloud_devices (id, class, box, sign, joined_epoch, moved_epoch) VALUES (?, ?, ?, ?, ?, ?)",
                [(d["id"], d["class"], d["box"], d["sign"], n, n) for d in devices.values()])
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

    def _seq_floor(self, listing: dict[str, str]) -> None:
        """A run cut short after an upload leaves that file's seq behind; never write another file under it."""
        top = _top_seq(listing, self.device_id)
        if top > int(self._get("seq")):
            with self.store._tx():
                self._set("seq", top)

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
                if not self._leftover(path):
                    raise ByobotError(f"{path} is already in the cloud folder with other content") from None
                self.folder.delete(path)
                self.folder.create(path, data)
        self.store.conn.execute("INSERT INTO cloud_read VALUES (?, ?, 'written') ON CONFLICT (path) DO UPDATE SET "
                                "checksum = excluded.checksum, outcome = excluded.outcome", (path, checksum(data)))

    def _leftover(self, path: str) -> bool:
        """A file of a start not yet committed, which a lagging listing hid from its cleanup; no device uses it."""
        n = int(self._get("starting") or 0)
        return bool(n) and path.startswith((f"keys/{n:04d}/", f"checkpoints/{n:04d}/"))

    def _segment(self, kind: str, seq: int, messages: list[dict], epoch: int | None = None) -> str:
        epoch = self.epoch if epoch is None else epoch
        path = segment.name(kind, epoch, seq, self.device_id)
        self._write(path, segment.write(kind, self.store.store_id, self.device_id, epoch, seq, messages,
                                        self.key(epoch), self.identity))
        return path

    # -- epochs ------------------------------------------------------------------

    def start_epoch(self) -> int:
        """Start the next epoch: a new key sealed to every active device, a checkpoint of the whole logbook, a
        plan, then the epoch file, which commits it."""
        listing = self.folder.list()
        top = max([int(p[7:11]) for p in listing if is_epoch_file(p)] + [self.epoch])
        n = int(self._get("starting") or 0)
        if not n or n < top or self._committed(n, listing):  # never reuse a number the folder has taken
            n = max(n, top) + 1
        with self.store._tx():
            self._set("starting", n)
        key = self.key(n) or new_key()  # a retried start seals the same key, so a leftover copy still opens
        store_id = self.store.store_id
        self._seq_floor(listing)
        for path in listing:  # what a start cut short left behind; no device uses it yet
            if path.startswith((f"keys/{n:04d}/", f"checkpoints/{n:04d}/", f"plan/{n:04d}-")):
                self.folder.delete(path)
        with self.store._tx():
            self.store.conn.execute("INSERT OR IGNORE INTO cloud_keys VALUES (?, ?)", (n, b64(key)))
        devices = [{k: d[k] for k in ("id", "class", "box", "sign")} for d in self.devices()]
        for d in devices:
            if d["id"] != self.device_id:
                self._write(files.key_path(n, d["id"]), files.key_file(self.identity, store_id, n, d, key))
        if self._get("recovery_key"):
            content = {"epoch_key": b64(key), "store_key": self.store.store_key.hex(), "device_id": self.device_id,
                       "keeper": self.identity.private()}
            self._write(files.recovery_path(n), files.recovery_file(unb64(self._get("recovery_key")), store_id, n,
                                                                    content))
        self.store.conn.execute("BEGIN")  # the checkpoint and its position from one snapshot
        try:
            events = self.store.events()
            position = self.store.position()
        finally:
            self.store.conn.execute("COMMIT")
        parts = [events[i:i + rules.MAX_BATCH] for i in range(0, len(events), rules.MAX_BATCH)]
        for i, part in enumerate(parts):  # uploads hold no write lock, so the hub and other writers go on
            self._segment("checkpoint", i, [{"events": {"events": part}}], n)
        with self.store._tx():
            self._set("epoch", n)
            self._set("checkpoint_position", position)
            self.store.conn.execute("DELETE FROM cloud_shown")
            self._unset("plan_digest")
            self._unset("epoch_pending")
            self._unset("missing_since")
            self._unset("closed")
        self.publish_plan()
        self._write(files.epoch_path(n), files.epoch_file(self.identity, store_id, n, self.device_id, devices,
                                                          len(parts)))
        with self.store._tx():
            self._unset("starting")
            self.store.conn.execute("DELETE FROM cloud_meta WHERE key LIKE 'removing:%'")
        return n

    def _committed(self, n: int, listing: dict[str, str]) -> bool:
        """Whether epoch ``n``'s file is in the folder, read directly since a listing can lag behind an upload."""
        return files.epoch_path(n) in listing or self.folder.read(files.epoch_path(n)) is not None

    # -- joining -------------------------------------------------------------------

    def join_code(self) -> dict:
        """A one-time code a phone scans to join: valid ten minutes, for one device. It holds no key."""
        secret = secrets.token_bytes(16)
        expires = self.clock() + JOIN_TTL_S
        with self.store._tx():
            self._set("join", json.dumps({"secret": b64(secret), "expires": expires}))
            self._unset("last_join")
        code = {"store_id": self.store.store_id, "secret": b64(secret), "keeper": self.identity.public()["sign"],
                "expires": expires}
        return {**code, "link": "byobot://cloud-join?" + urlencode(
            {"store": code["store_id"], "secret": code["secret"], "keeper": code["keeper"]})}

    def _admit(self, listing: dict[str, str]) -> dict:
        join = json.loads(self._get("join") or "null")
        if not join:
            return {}
        if self.clock() > join["expires"]:
            return self._joined({"join": "expired"}, listing)
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
        if len(valid) > 1:
            return self._joined({"join": "refused: two devices answered one code; show a new code"}, listing)
        device = {k: valid[0][k] for k in ("id", "class", "box", "sign")}
        path = files.key_path(self.epoch, device["id"])
        try:  # the code stays good until the phone's key is in the folder
            self._write(path, files.key_file(self.identity, self.store.store_id, self.epoch, device,
                                             self.key(self.epoch)))
        except ByobotError:  # an earlier try landed and its answer was lost
            try:
                landed = files.key_device(self.folder.read(path) or b"", self.identity.public()["sign"],
                                          self.store.store_id, self.epoch)
            except CloudError:
                landed = None
            if landed != device:
                raise
        return self._joined({"join": "admitted", "device": device["id"]}, listing, device)

    def _joined(self, outcome: dict, listing: dict[str, str], device: dict | None = None) -> dict:
        """End the join: enrol the device if one was admitted, keep the outcome for `cloud join`, and delete the
        device files no one admitted, so those phones can answer a new code."""
        with self.store._tx():
            if device:
                self._enrol(device, self.epoch)
            self._unset("join")
            self._set("last_join", json.dumps(outcome))
        known = {d["id"] for d in self.devices()} | {d["id"] for d in self.devices(removed=True)}
        for path in listing:
            if path.startswith("devices/") and path.endswith(".json") and path[8:-5] not in known:
                self.folder.delete(path)
        return outcome

    def last_join(self) -> dict | None:
        """How the last join ended, once it has."""
        return None if self._get("join") else json.loads(self._get("last_join") or "null")

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
                if where["kind"] == "segment" and where["device_id"] != self.device_id and NEWER not in outcome:
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
        shown = self._shown if where["epoch"] == self.epoch else lambda ids: None  # a retired epoch's files go
        apply(self.store, writer, device["class"], messages, shown, lambda i, why: refused.append((i, why)))
        with self.store._tx():
            self.store.conn.executemany("INSERT OR REPLACE INTO cloud_refused VALUES (?, ?, ?)",
                                        [(path, i, why) for i, why in refused])
        erased = set(self.store.erased())
        if erased and any(e.get("record", {}).get("kind") and (e["record"]["kind"], e["record"]["id"]) in erased
                          for m in messages for e in _events(m)) or self._erased_samples(messages):
            with self.store._tx():
                self._set("epoch_pending", "erase")  # it carried a record already erased here
        with self.store._tx():
            self.store.conn.execute("UPDATE cloud_devices SET moved_epoch = max(moved_epoch, ?), last_seq = "
                                    "max(coalesce(last_seq, 0), ?) WHERE id = ?",
                                    (where["epoch"], where["seq"], writer))
            if writer != self.device_id:
                self._set(f"heard:{writer}", self.clock())
        return "read"

    def _erased_samples(self, messages: list[dict]) -> bool:
        for m in messages:
            body = m.get("samples")
            for s in body.get("samples", []) if isinstance(body, dict) else []:
                try:
                    rid = sources.record_id(self.store, "apple_health", sources.sample_type(s["type"]), s["source_id"])
                except (KeyError, TypeError, ValueError, ByobotError):
                    continue
                found = self.store.record("source_record", rid)
                if found and found["erased"]:
                    return True
        return False

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
                seq = self._next_seq()
            self._segment("segment", seq, [{"events": {"events": part}}])
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
        """Each relay's segments still in the folder that the keeper has read, by seq (a relay's seqs never repeat
        across epochs), and the samples it refused."""
        out: dict[str, dict] = {d["id"]: {"read": []} for d in self.devices() if d["class"] == "relay"}
        refused: dict[str, list] = {}
        for path, line, why in self.store.conn.execute("SELECT * FROM cloud_refused ORDER BY path, line"):
            refused.setdefault(path, []).append((line, why))
        for path, outcome in self.store.conn.execute("SELECT path, outcome FROM cloud_read WHERE path LIKE "
                                                     "'segments/%' ORDER BY path"):
            where = segment.parse_name(path)
            if where and where["device_id"] in out and outcome == "read":
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
            seq = self._next_seq()
        path = self._segment("plan", seq, messages)
        with self.store._tx():
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
        report = self._sync()
        with self.store._tx():
            self._set("synced_at", self.clock())
            self._unset("error")
        return report

    def failed(self, message: str) -> None:
        """Keep why the last sync failed, for `cloud status`."""
        with self.store._tx():
            self._set("error", message)

    def _sync(self) -> dict:
        if self._get("read_by") != __version__:  # upgraded: read again what an older byobot couldn't
            with self.store._tx():
                self.store.conn.execute("DELETE FROM cloud_read WHERE instr(outcome, ?)", (NEWER,))
                self._set("read_by", __version__)
        if self._get("closing"):
            return {"epoch": self.epoch, "closed": self.forget()}
        listing = self.folder.list()
        self._seq_floor(listing)
        report: dict[str, Any] = {}
        if self._get("closed"):  # turned on again after `cloud off --delete yes`
            report["reseeded"] = self.start_epoch()
            listing = self.folder.list()
        there = files.epoch_path(self.epoch) in listing
        starting = self._get("starting")
        if starting and (starting != str(self.epoch) or not there) or not there and self._gone():
            report["reseeded"] = self.start_epoch()  # a start was cut short, or the folder was deleted
            listing = self.folder.list()
        elif there and (starting or self._get("missing_since")):
            with self.store._tx():
                self._unset("missing_since")
                self._unset("starting")
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

    def _gone(self) -> bool:
        """Whether the current epoch's file is really gone, not just missing from a listing that lags behind
        another process: still missing on a fresh listing a while after it was first missed."""
        since = self._get("missing_since")
        if since is None:
            with self.store._tx():
                self._set("missing_since", self.clock())
            return False
        if self.clock() - float(since) < MISSING_S:
            return False
        if hasattr(self.folder, "refresh"):
            self.folder.refresh()
        if files.epoch_path(self.epoch) in self.folder.list():
            with self.store._tx():
                self._unset("missing_since")
            return False
        return True

    def compact(self) -> int:
        """Rewrite the folder into a new epoch now; the old one is deleted once every device has moved."""
        return self.start_epoch()

    def remove(self, device_id: str) -> int:
        """Retire a device: a new epoch whose key isn't sealed to it. What it already read, it keeps."""
        device = self.device(device_id)
        if device is None or device["removed"] and not self._get(f"removing:{device_id}"):
            raise ByobotError(f"no device {device_id} in this cloud logbook")
        if device["class"] == "keeper":
            raise ByobotError("the keeper can't remove itself")
        with self.store._tx():  # until an epoch without it commits, running this again finishes the removal
            self.store.conn.execute("UPDATE cloud_devices SET removed = 1 WHERE id = ?", (device_id,))
            self._set(f"removing:{device_id}", 1)
            self.store.conn.execute("DELETE FROM cloud_keys WHERE epoch > ?", (self.epoch,))  # it may hold that one
        self.folder.delete(files.device_path(device_id))
        return self.start_epoch()

    def off(self) -> None:
        """Stop reading and writing the folder; `cloud setup` turns it on again."""
        with self.store._tx():
            self._set("off", 1)

    def on(self) -> None:
        with self.store._tx():
            self._unset("off")

    @property
    def is_off(self) -> bool:
        return self._get("off") is not None

    def forget(self) -> int:
        """Close the folder and turn Drive sync off. A last epoch lists only this keeper, so every phone learns it
        was taken off and stops writing; every other file is deleted, and with them the keys and devices kept
        here. The logbook is untouched, and `cloud setup` starts a new epoch past the closed one."""
        n = int(self._get("closing") or 0) or max([int(p[7:11]) for p in self.folder.list() if is_epoch_file(p)]
                                                  + [self.epoch]) + 1
        with self.store._tx():  # sync finishes a close that failed partway, so phones and this Mac agree
            self._set("closing", n)
        path = files.epoch_path(n)
        keeper = [{"id": self.device_id, "class": "keeper", **self.identity.public()}]
        self._write(path, files.epoch_file(self.identity, self.store.store_id, n, self.device_id, keeper, 0))
        listing = [p for p in self.folder.list() if p != path]
        for p in listing:
            self.folder.delete(p)
        with self.store._tx():
            for table in ("cloud_keys", "cloud_read", "cloud_shown", "cloud_refused"):
                self.store.conn.execute(f"DELETE FROM {table}")
            self.store.conn.execute("DELETE FROM cloud_devices WHERE class != 'keeper'")
            self.store.conn.execute("DELETE FROM cloud_meta WHERE key LIKE 'heard:%' OR key IN ('join', 'last_join', "
                                    "'plan_digest', 'epoch_pending', 'missing_since', 'starting', 'error', 'closing') "
                                    "OR key LIKE 'removing:%'")
            self._set("epoch", n)
            self._set("checkpoint_position", 0)
            self._set("closed", 1)
            self._set("off", 1)
        return len(listing)

    def _new_epoch_reason(self, listing: dict[str, str]) -> str | None:
        last_erase = self.store.conn.execute("SELECT coalesce(max(seq), 0) FROM events WHERE op = 'erase'").fetchone()[0]
        if last_erase > int(self._get("checkpoint_position")):
            return "erase"
        if self._get("epoch_pending"):
            return self._get("epoch_pending")
        if sum(not p.startswith("checkpoints/") for p in listing) > MAX_FILES and not self._waiting():
            return "compact"
        return None

    def _newer(self) -> bool:
        found = self.store.conn.execute("SELECT 1 FROM cloud_read WHERE instr(outcome, ?) LIMIT 1", (NEWER,))
        return found.fetchone() is not None

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
        if self._newer():
            return {"cleanup_waits_on": ["an upgrade of byobot on this Mac"]}
        for path in old:
            self.folder.delete(path)
        with self.store._tx():
            self.store.conn.execute("DELETE FROM cloud_keys WHERE epoch < ?", (current,))
            self.store.conn.executemany("DELETE FROM cloud_read WHERE path = ?", [(p,) for p in old])
            self.store.conn.executemany("DELETE FROM cloud_refused WHERE path = ?", [(p,) for p in old])
        return {"cleaned": len(old)}

    def local_status(self) -> dict:
        """What this store knows about the folder, without asking Drive."""
        synced = self._get("synced_at")
        out: dict[str, Any] = {"store_id": self.store.store_id, "epoch": self.epoch,
                               "synced_at": _when(synced), "recovery": bool(self._get("recovery_key"))}
        if self.is_off:
            out["off"] = True
        if self._get("error"):
            out["error"] = self._get("error")
        if self._newer():
            out["upgrade"] = "a device writes with a newer byobot; upgrade byobot on this Mac to read it"
        out["devices"] = [{**{k: d[k] for k in ("id", "class", "joined_epoch", "moved_epoch", "last_seq")},
                           "last_heard": _when(synced if d["class"] == "keeper" else self._get(f"heard:{d['id']}"))}
                          for d in self.devices()]
        return out

    def status(self) -> dict:
        out = self.local_status()
        listing = self.folder.list()
        out["files"] = len(listing)
        if hasattr(self.folder, "size"):
            out["size_bytes"] = self.folder.size()
        out["cleanup_waits_on"] = self._waiting() if any((e := _epoch_of(p)) is not None and e < self.epoch
                                                         for p in listing) else []
        return out


def _when(stamp: str | None) -> str | None:
    return stamp and datetime.fromtimestamp(float(stamp)).astimezone().isoformat(timespec="seconds")


def _tables(store: Store) -> None:
    for statement in DDL.strip().split(";"):
        if statement.strip():
            store.conn.execute(statement)


def due(store: Store, every: float, clock: Callable[[], float] = time.time) -> bool:
    """Whether a background sync is due: this store keeps a folder, Drive sync isn't off, and nothing tried in the
    last ``every`` seconds. A try stamped in the future (the clock ran ahead) counts as due. Reads only, so a store
    without Drive sync is left as it is."""
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cloud_meta'").fetchone():
        return False
    meta = dict(store.conn.execute("SELECT key, value FROM cloud_meta WHERE key IN ('device_id', 'off', 'tried_at')"))
    since = clock() - float(meta.get("tried_at", 0))
    return "device_id" in meta and "off" not in meta and (since >= every or since < 0)


def tried(store: Store, clock: Callable[[], float] = time.time) -> None:
    store.conn.execute("INSERT INTO cloud_meta VALUES ('tried_at', ?) ON CONFLICT (key) DO UPDATE SET value = "
                       "excluded.value", (str(clock()),))


def is_keeper(store: Store) -> bool:
    """Whether this store keeps a cloud folder."""
    _tables(store)
    return store.conn.execute("SELECT 1 FROM cloud_meta WHERE key = 'device_id'").fetchone() is not None


def _top_seq(listing: dict[str, str], device_id: str) -> int:
    """The keeper's next seq past every file of its own in the folder."""
    own = [segment.parse_name(p) for p in listing if p.startswith((f"segments/{device_id}/", "plan/"))]
    return max([w["seq"] for w in own if w and w["seq"] < SEQ_CEILING] + [0]) + 1


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
