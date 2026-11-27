"""The keeper: the one device per store that holds the whole byobot logbook and looks after the cloud folder.

Each sync it admits a device that joined with the current join code, reads every segment it hasn't read
(a relay's through the same accept rules as the hub, its own in full), publishes what it holds that the folder
doesn't show yet, and publishes the plan when it changed. Its cloud state lives in the store's ``cloud_`` tables.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from .. import __version__
from ..core import relay as rules
from ..core import sources
from ..core.store import Store, stamp
from ..link import pairing_link
from ..util import ByobotError, ValidationFailed
from . import files, segment
from .crypto import CloudError, Identity, b64, dumps, new_key, proof_matches, recovery_code, recovery_key, unb64
from .folder import Exists, Folder, checksum
from .state import TABLES, State

JOIN_TTL_S = 600
MAX_FILES = 500  # past this, the keeper compacts into a new epoch
PLAN_BEHIND, PLAN_AHEAD = 1, 13
MISSING_S = 30  # past Drive's change-feed lag
SEQ_CEILING = 10**9  # a name past this wasn't the keeper's: it writes one seq at a time
NEWER = "newer byobot"  # in a skip reason: kept in the folder until this byobot is upgraded

class Keeper:
    def __init__(self, store: Store, folder: Folder, clock: Callable[[], float] = time.time):
        self.store, self.folder, self.clock = store, folder, clock
        self.state = State(store.conn)
        if self.state.get("identity") is None:
            raise ByobotError("the cloud logbook isn't set up for this store; run byobot cloud setup")
        self.device_id = store.device_id  # the keeper is the Mac, by the one id it has
        self.identity = Identity.from_private(json.loads(self.state.get("identity")))
        self.recovery_code: str | None = None

    # -- setup -----------------------------------------------------------------

    @classmethod
    def setup(cls, store: Store, folder: Folder, clock: Callable[[], float] = time.time,
              recovery: bool = False) -> Keeper:
        """Make this store the folder's keeper and start the first epoch. With ``recovery``, ``recovery_code`` on
        the result is shown once and opens every later epoch's recovery key."""
        if is_keeper(store):
            raise ByobotError("this store is already the cloud logbook's keeper")
        listing = folder.list()
        if any(p.startswith("epochs/") for p in listing) and not _closed(folder, listing):
            raise ByobotError("this cloud folder already has a keeper; run `byobot cloud recover` with the recovery code instead")
        identity = Identity.generate()
        code = recovery_code() if recovery else None
        meta = [("identity", json.dumps(identity.private())), ("epoch", "0"), ("seq", "1"),
                ("checkpoint_position", "0")]
        if code:
            meta += [("recovery_key", b64(recovery_key(code, store.store_id))), ("recovery_unshown", code)]
        with store.transaction():
            State(store.conn).add(meta)
            State(store.conn).enrol([(store.device_id, "keeper", identity.public()["box"], identity.public()["sign"],
                                      1, 1)])
        keeper = cls(store, folder, clock)
        keeper.start_epoch()
        keeper.recovery_code = keeper.unshown_code()
        return keeper

    @classmethod
    def recover(cls, store: Store, folder: Folder, code: str, clock: Callable[[], float] = time.time) -> Keeper:
        """Become the keeper again from the folder and the recovery code: on a new Mac with an empty store, or on
        one restored from an export of this store. Reads every epoch the code opens, then starts a new one."""
        if is_keeper(store):
            raise ByobotError("this store is already the cloud logbook's keeper")
        listing = folder.list()
        opened: dict[int, dict] = {}
        store_id, wrong = None, False
        for epoch in _recovery_epochs(listing):
            data = folder.read(files.recovery_path(epoch)) or b""
            try:
                found = json.loads(data)["store_id"]
                opened[epoch] = files.open_recovery(data, recovery_key(code, found), found, epoch)
                store_id = found
            except (ValueError, KeyError, TypeError, CloudError) as exc:
                wrong = wrong or "recovery code" in str(exc)
        if wrong and not opened:
            raise ByobotError("that recovery code doesn't open this store")
        identity = epoch_body = None
        for n in sorted(opened, reverse=True):
            identity = Identity.from_private(opened[n]["keeper_private"])
            try:
                epoch_body = files.read_epoch(folder.read(files.epoch_path(n)) or b"", identity.public()["sign"],
                                              store_id, n)
                break
            except CloudError:
                continue
        if epoch_body is None:
            raise ByobotError("this cloud folder has no recovery key the code opens; restore from an export instead")
        if any(e > n and files.epoch_path(e) in listing for e in _recovery_epochs(listing)):
            raise ByobotError("a newer recovery code replaced that one; use the code `byobot cloud rotate --recovery` "
                              "showed last")
        top = opened[n]
        devices = {d["device_id"]: d for d in epoch_body["devices"]}
        for path in listing:  # admitted since the epoch file was written: only a key file names them
            if path.startswith(f"keys/{files.epoch_dir(n)}/") and not path.endswith("/recovery.key"):
                try:
                    d = files.key_device(folder.read(path) or b"", identity.public()["sign"], store_id, n)
                except CloudError:
                    continue
                devices.setdefault(d["device_id"], d)
        if not store.is_empty():
            if store.store_id != store_id or store.store_key.hex() != top["store_key"]:
                raise ByobotError("this store isn't the one the cloud folder belongs to")
        else:
            store.adopt(store_id, top["store_key"])
        meta = [("identity", json.dumps(identity.private())), ("epoch", n),
                ("seq", _top_seq(listing, top["keeper_device_id"])), ("checkpoint_position", 0),
                ("recovery_key", b64(recovery_key(code, store_id)))]
        mine = store.device_id
        state = State(store.conn)
        with store.transaction():
            store.set_device_id(top["keeper_device_id"])  # this Mac takes the keeper's place, and its id
            state.add(meta)
            state.add_keys((e, c["epoch_key"]) for e, c in opened.items())
            state.enrol((*(d[k] for k in files.DEVICE), n, n) for d in devices.values())
        keeper = cls(store, folder, clock)
        keeper._read(listing)
        outcomes = state.outcomes()
        own = {p for p in listing if (w := segment.parse_name(p)) and w["kind"] != "plan" and w["epoch"] == n
               and w.get("device_id", top["keeper_device_id"]) == top["keeper_device_id"]}
        lost = sorted(p for p in own | {segment.name("checkpoint", n, i) for i in range(epoch_body["checkpoint"])}
                      if outcomes.get(p) != "read")
        if lost:  # nothing kept here, so it can run again
            with store.transaction():
                state.clear(*TABLES)
                state.leave()
                store.set_device_id(mine)
            raise ByobotError(f"part of the logbook in Drive can't be read ({lost[0]}: "
                              f"{outcomes.get(lost[0], 'missing')}); this store may hold only part of it, so restore "
                              "from an export instead, or upgrade byobot and run `byobot cloud recover` again")
        keeper.start_epoch()
        return keeper

    # -- state -------------------------------------------------------------------

    @property
    def epoch(self) -> int:
        return int(self.state.get("epoch"))

    def key(self, epoch: int) -> bytes | None:
        found = self.state.key(epoch)
        return found and unb64(found)

    def devices(self, removed: bool = False) -> list[dict]:
        return self.state.devices(removed)

    def device(self, device_id: str) -> dict | None:
        return self.state.device(device_id)

    def _enrol(self, device: dict, epoch: int) -> None:
        self.state.enrol([(*(device[k] for k in files.DEVICE), epoch, epoch)])

    def _rejoin(self, device: dict) -> None:
        """Take back a removed device. Its relay counts seqs from the start again, so its older files go unread."""
        self.state.rejoin(device["device_id"], device["box"], device["sign"], self.epoch)
        self.state.forget(prefix=f"segments/{device['device_id']}/")
        self.state.set(f"rejoined:{device['device_id']}", self.epoch)

    def _seq_floor(self, listing: dict[str, str]) -> None:
        """A run cut short after an upload leaves that file's seq behind; never write another file under it."""
        top = _top_seq(listing, self.device_id)
        if top > int(self.state.get("seq")):
            with self.store.transaction():
                self.state.set("seq", top)

    def _next_seq(self) -> int:
        seq = int(self.state.get("seq"))
        self.state.set("seq", seq + 1)
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
        self.state.mark(path, checksum(data), "written")

    def _leftover(self, path: str) -> bool:
        """A file of a start not yet committed, which a lagging listing hid from its cleanup; no device uses it."""
        n = int(self.state.get("starting") or 0)
        d = files.epoch_dir(n)
        return bool(n) and path.startswith((f"keys/{d}/", f"checkpoints/{d}/"))

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
        top = max([files.epoch_number(p) for p in listing if files.is_epoch_file(p)] + [self.epoch])
        n = int(self.state.get("starting") or 0)
        if not n or n < top or self._committed(n, listing):  # never reuse a number the folder has taken
            n = max(n, top) + 1
        with self.store.transaction():
            self.state.set("starting", n)
        key = self.key(n) or new_key()  # a retried start seals the same key, so a leftover copy still opens
        store_id = self.store.store_id
        self._seq_floor(listing)
        d = files.epoch_dir(n)
        for path in listing:  # what a start cut short left behind; no device uses it yet
            if path.startswith((f"keys/{d}/", f"checkpoints/{d}/", f"plan/{d}-")):
                self.folder.delete(path)
        with self.store.transaction():
            self.state.add_keys([(n, b64(key))])
        devices = [_wire(d) for d in self.devices()]
        for d in devices:
            if d["device_id"] != self.device_id:
                self._write(files.key_path(n, d["device_id"]), files.key_file(self.identity, store_id, n, d, key))
        if self.state.get("recovery_key"):
            content = {"epoch_key": b64(key), "store_key": self.store.store_key.hex(),
                       "keeper_device_id": self.device_id, "keeper_private": self.identity.private()}
            self._write(files.recovery_path(n), files.recovery_file(unb64(self.state.get("recovery_key")), store_id, n,
                                                                    content))
        with self.store.snapshot():  # the checkpoint and its position from one snapshot
            events = self.store.events()
            position = self.store.position()
        parts = _parts(events)
        for i, part in enumerate(parts):  # uploads hold no write lock, so the hub and other writers go on
            self._segment("checkpoint", i, [{"events": {"events": part}}], n)
        with self.store.transaction():
            self.state.set("epoch", n)
            self.state.set("checkpoint_position", position)
            self.state.unset("published_position")
            self.state.unset("plan_digest")
            self.state.unset("epoch_pending")
            self.state.unset("missing_since")
            self.state.unset("closed")
        self.publish_plan()
        self._write(files.epoch_path(n), files.epoch_file(self.identity, store_id, n, self.device_id, devices,
                                                          len(parts), previous=self._previous()))
        with self.store.transaction():
            self.state.unset("starting")
            self.state.unset("previous_identity")
            self.state.unset(prefixes=["removing:"])
        return n

    def _previous(self) -> Identity | None:
        """The keys the keeper signed with before `rotate`, until an epoch file hands over from them."""
        kept = self.state.get("previous_identity")
        return Identity.from_private(json.loads(kept)) if kept else None

    def rotate(self, recovery: bool = False) -> int:
        """Sign with new keys from a new epoch on, whose file the old keys hand over to, so every phone follows.
        With ``recovery``, a new recovery code too, on ``recovery_code``; the old code's recovery keys are deleted
        once the new epoch is in. A retry of a rotation cut short keeps the code it made, which was never shown."""
        new = Identity.generate()
        pending = self.unshown_code() if self.state.get("previous_identity") else None
        code = pending or (recovery_code() if recovery else None)
        with self.store.transaction():  # a retry of a rotation cut short still hands over from the keys phones trust
            if self.state.get("previous_identity") is None:
                self.state.set("previous_identity", json.dumps(self.identity.private()))
            self.state.set("identity", json.dumps(new.private()))
            if code:
                self.state.set("recovery_key", b64(recovery_key(code, self.store.store_id)))
                self.state.set("recovery_unshown", code)
            self.state.set_device_keys(self.device_id, new.public()["box"], new.public()["sign"])
        self.identity = new
        self.folder.delete(files.device_path(self.device_id))
        n = self.start_epoch()
        if code:
            for path in self.folder.list():
                if path.endswith(f"/{files.RECOVERY}.key") and path != files.recovery_path(n):
                    self.folder.delete(path)
        self._publish_device()
        self.recovery_code = code
        return n

    def unshown_code(self) -> str | None:
        """A recovery code setup or `rotate` made that no command has shown yet, as one was cut short."""
        return self.state.get("recovery_unshown")

    def code_shown(self) -> None:
        with self.store.transaction():
            self.state.unset("recovery_unshown")

    def _committed(self, n: int, listing: dict[str, str]) -> bool:
        """Whether epoch ``n``'s file is in the folder, read directly since a listing can lag behind an upload."""
        return files.epoch_path(n) in listing or self.folder.read(files.epoch_path(n)) is not None

    # -- joining -------------------------------------------------------------------

    def join_code(self, client: str | None = None) -> dict:
        """A one-time code a phone scans to join: valid ten minutes, for one device. It holds no key. ``client`` is
        the Google client id the phone signs in with."""
        secret = secrets.token_bytes(16)
        expires = self.clock() + JOIN_TTL_S
        sealed = self._sealed(self.folder.list()) if self.folder is not None and self.state.get("join") else None
        with self.store.transaction():
            if sealed:  # the code this one replaces let that phone in
                self._take(sealed)
            self.state.set("join", json.dumps({"secret": b64(secret), "expires": expires}))
            self.state.unset("last_join")
        code = {"store_id": self.store.store_id, "secret": b64(secret), "keeper_key": self.identity.public()["sign"],
                "expires": expires}
        return {**code, "link": pairing_link(store_id=code["store_id"], secret=code["secret"],
                                             keeper_key=code["keeper_key"], client=client)}

    def _admit(self, listing: dict[str, str]) -> dict:
        join = json.loads(self.state.get("join") or "null")
        if not join:
            return {}
        if self.clock() > join["expires"]:
            return self._joined({"join": "expired"}, listing, join)
        secret = unb64(join["secret"])
        active = {d["id"] for d in self.devices()}  # a removed phone may answer a new code with the same id
        valid = []
        for path in listing:
            if not path.startswith("devices/") or not path.endswith(".json"):
                continue
            device_id = path[len("devices/"):-len(".json")]
            if device_id in active:
                continue
            data = self.folder.read(path)
            try:
                found = files.read_device(data or b"", self.store.store_id, device_id)
            except CloudError:
                continue
            public = {"box": found["box"], "sign": found["sign"]}
            if found["device_class"] == "relay" and proof_matches(secret, self.store.store_id, device_id, public,
                                                                  found.get("proof")):
                valid.append(found)
        if not valid:
            return {}
        if len(valid) > 1:
            return self._joined({"join": "refused: two devices answered one code; show a new code"}, listing, join)
        device = {k: valid[0][k] for k in files.DEVICE}
        path = files.key_path(self.epoch, device["device_id"])
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
        return self._joined({"join": "admitted", "device": device["device_id"]}, listing, join, device)

    def _joined(self, outcome: dict, listing: dict[str, str], join: dict, device: dict | None = None) -> dict:
        """End ``join``: enrol the device if one was admitted, or one whose key file a sync cut short already
        wrote, keep the outcome for `cloud join`, and delete the device files no one admitted, so those phones can
        answer a new code. A newer code shown meanwhile is left as it is."""
        if device is None and (device := self._sealed(listing)):
            outcome = {"join": "admitted", "device": device["device_id"]}
        with self.store.transaction():
            if device:
                self._take(device)
            replaced = json.loads(self.state.get("join") or "null") != join
            if not replaced:
                self.state.unset("join")
                self.state.set("last_join", json.dumps(outcome))
        if replaced:
            return outcome
        known = {d["id"] for d in self.devices()} | {d["id"] for d in self.devices(removed=True)}
        for path in listing:
            if path.startswith("devices/") and path.endswith(".json") and path[8:-5] not in known:
                self.folder.delete(path)
        return outcome

    def _sealed(self, listing: dict[str, str]) -> dict | None:
        """A device this epoch's key was sealed to that isn't in yet, if its device file still matches; any other
        such key file is deleted."""
        active = {d["id"] for d in self.devices()}
        prefix = f"keys/{files.epoch_dir(self.epoch)}/"
        for path in listing:
            device_id = path[len(prefix):-len(".key")]
            if not path.startswith(prefix) or not path.endswith(".key") or path == files.recovery_path(self.epoch) \
                    or device_id in active:
                continue
            try:
                sealed = files.key_device(self.folder.read(path) or b"", self.identity.public()["sign"],
                                          self.store.store_id, self.epoch)
                found = files.read_device(self.folder.read(files.device_path(device_id)) or b"",
                                          self.store.store_id, device_id)
            except CloudError:
                found = None
            if found and sealed == {k: found[k] for k in files.DEVICE}:
                return sealed
            self.folder.delete(path)
        return None

    def _take(self, device: dict) -> None:
        if self.device(device["device_id"]):
            self._rejoin(device)
        else:
            self._enrol(device, self.epoch)

    def last_join(self) -> dict | None:
        """How the last join ended, once it has."""
        return None if self.state.get("join") else json.loads(self.state.get("last_join") or "null")

    # -- reading -------------------------------------------------------------------

    def _read(self, listing: dict[str, str]) -> dict:
        report: dict[str, Any] = {"read": 0, "skipped": []}
        held = self.state.checksums()
        for path, sum_ in listing.items():
            where = segment.parse_name(path)
            if where is None or held.get(path) == sum_:
                continue
            outcome = self._read_one(path, where)
            with self.store.transaction():
                self.state.mark(path, sum_, outcome)
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
        if where["epoch"] < int(self.state.get(f"rejoined:{writer}") or 0):
            return "from before this device joined again"
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
        rejected: list[tuple[int, str]] = []
        apply(self.store, writer, device["class"], messages, lambda i, why: rejected.append((i, why)))
        with self.store.transaction():
            self.state.refuse((path, i, why) for i, why in rejected)
        erased = set(self.store.erased())
        if erased and any(e.get("record", {}).get("kind") and (e["record"]["kind"], e["record"]["id"]) in erased
                          for m in messages for e in _events(m)) or self._erased_samples(messages):
            with self.store.transaction():
                self.state.set("epoch_pending", "erase")  # it carried a record already erased here
        with self.store.transaction():
            self.state.moved(writer, where["epoch"], where.get("seq", 0))
            if writer != self.device_id:
                self.state.seen(writer, stamp(datetime.fromtimestamp(self.clock()).astimezone()))
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

    # -- writing -------------------------------------------------------------------

    def publish_events(self) -> int:
        """Publish every event that arrived since the last publish: its own, the source events it derived, and what
        phones sent, by the hub or Drive. A relay's events come back to it; readers skip events they hold."""
        since = self.published()
        with self.store.snapshot():
            events = self.store.events(since=since)
            positions = [r["seq"] for r in self.store.rows("SELECT seq FROM events WHERE seq > ? ORDER BY seq",
                                                           (since,))]
        done = 0
        for part in _parts(events):
            with self.store.transaction():
                seq = self._next_seq()
            self._segment("segment", seq, [{"events": {"events": part}}])
            done += len(part)
            with self.store.transaction():
                self.state.set("published_position", positions[done - 1])
        return len(events)

    def published(self) -> int:
        """The arrival position the folder shows events up to: the checkpoint's, then each segment's."""
        return max(int(self.state.get("checkpoint_position")), int(self.state.get("published_position") or 0))

    def plan(self) -> dict:
        """The plan window every relay reads, with each relay's latest event held and rejected events."""
        today = self.store.wall().date()
        start, end = today - timedelta(days=PLAN_BEHIND), today + timedelta(days=PLAN_AHEAD)
        window = rules.plan_window(self.store, start.isoformat(), end.isoformat())
        relays = [d["id"] for d in self.devices() if d["class"] == "relay"]
        window["received"] = {d: r for d in relays if (r := self.store.received(d))}
        window["rejected"] = [r for d in relays for r in self.store.rejections(d)]
        return window

    def receipts(self) -> dict:
        """Each relay's segments still in the folder that the keeper has read, by seq (a relay's seqs never repeat
        across epochs), and the samples messages it rejected."""
        out: dict[str, dict] = {d["id"]: {"read": []} for d in self.devices() if d["class"] == "relay"}
        rejected: dict[str, list] = {}
        for path, line, why in self.state.refused():
            rejected.setdefault(path, []).append((line, why))
        for path, outcome in self.state.outcomes("segments/").items():
            where = segment.parse_name(path)
            if where and where["device_id"] in out and outcome == "read":
                mine = out[where["device_id"]]
                mine["read"].append(where["seq"])
                for line, why in rejected.get(path, []):
                    mine.setdefault("rejected", []).append({"seq": where["seq"], "line": line, "reason": why})
        return out

    def publish_plan(self) -> bool:
        window = self.plan()
        messages = [{"plan": window}, {"receipts": self.receipts()}]
        digest = checksum(dumps(messages))
        if self.state.get("plan_digest") == digest:
            return False
        old = [p for p in self.folder.list() if p.startswith("plan/")]
        with self.store.transaction():
            seq = self._next_seq()
        path = self._segment("plan", seq, messages)
        with self.store.transaction():
            self.state.set("plan_digest", digest)
        for p in old:
            if p != path:
                self.folder.delete(p)
        return True

    def _publish_device(self) -> None:
        path = files.device_path(self.device_id)
        if path not in self.folder.list():
            self._write(path, files.device_file(self.store.store_id, {"device_id": self.device_id,
                                                                      "device_class": "keeper",
                                                                      **self.identity.public()}))

    # -- sync ----------------------------------------------------------------------

    def sync(self) -> dict:
        report = self._sync()
        with self.store.transaction():
            self.state.set("synced_at", self.clock())
            self.state.unset("error")
        return report

    def failed(self, message: str) -> None:
        """Keep why the last sync failed, for `cloud status`."""
        with self.store.transaction():
            self.state.set("error", message)

    def _sync(self) -> dict:
        if self.state.get("read_by") != __version__:  # upgraded: read again what an older byobot couldn't
            with self.store.transaction():
                self.state.forget_outcomes(NEWER)
                self.state.set("read_by", __version__)
        if self.state.get("closing"):
            return {"epoch": self.epoch, "closed": self.forget()}
        listing = self.folder.list()
        self._seq_floor(listing)
        report: dict[str, Any] = {}
        if self.state.get("closed"):  # turned on again after `cloud off --delete`
            report["reseeded"] = self.start_epoch()
            listing = self.folder.list()
        there = files.epoch_path(self.epoch) in listing
        starting = self.state.get("starting")
        if starting and (starting != str(self.epoch) or not there) or not there and self._gone():
            report["reseeded"] = self.start_epoch()  # a start was cut short, or the folder was deleted
            listing = self.folder.list()
        elif there and (starting or self.state.get("missing_since")):
            with self.store.transaction():
                self.state.unset("missing_since")
                self.state.unset("starting")
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
        since = self.state.get("missing_since")
        if since is None:
            with self.store.transaction():
                self.state.set("missing_since", self.clock())
            return False
        if self.clock() - float(since) < MISSING_S:
            return False
        if hasattr(self.folder, "refresh"):
            self.folder.refresh()
        if files.epoch_path(self.epoch) in self.folder.list():
            with self.store.transaction():
                self.state.unset("missing_since")
            return False
        return True

    def remove(self, device_id: str) -> int:
        """Retire a device: a new epoch whose key isn't sealed to it. What it already read, it keeps."""
        device = self.device(device_id)
        if device is None or device["removed"] and not self.state.get(f"removing:{device_id}"):
            raise ByobotError(f"no device {device_id} in this cloud logbook")
        if device["class"] == "keeper":
            raise ByobotError("the keeper can't remove itself")
        with self.store.transaction():  # until an epoch without it commits, running this again finishes the removal
            self.state.remove(device_id)
            self.state.set(f"removing:{device_id}", 1)
            self.state.drop_keys(after=self.epoch)  # it may hold that one
        self.folder.delete(files.device_path(device_id))
        return self.start_epoch()

    def off(self) -> None:
        """Stop reading and writing the folder; `cloud setup` turns it on again."""
        with self.store.transaction():
            self.state.set("off", 1)

    def on(self) -> None:
        with self.store.transaction():
            self.state.unset("off")

    @property
    def is_off(self) -> bool:
        return self.state.get("off") is not None

    @property
    def is_closed(self) -> bool:
        """Whether `cloud off --delete` closed the folder, so setup may start again in any account."""
        return self.state.get("closed") is not None

    def forget(self) -> int:
        """Close the folder and turn Drive sync off. A last epoch lists only this keeper, so every phone learns it
        was taken off and stops writing; every other file is deleted, and with them the keys and devices kept
        here. The logbook is untouched, and `cloud setup` starts a new epoch past the closed one."""
        n = int(self.state.get("closing") or 0) or max(
            [files.epoch_number(p) for p in self.folder.list() if files.is_epoch_file(p)] + [self.epoch]) + 1
        with self.store.transaction():  # sync finishes a close that failed partway, so phones and this Mac agree
            self.state.set("closing", n)
        path = files.epoch_path(n)
        keeper = [{"device_id": self.device_id, "device_class": "keeper", **self.identity.public()}]
        try:
            self._write(path, files.epoch_file(self.identity, self.store.store_id, n, self.device_id, keeper, 0,
                                               previous=self._previous(), closed=True))
        except ByobotError:  # an older byobot began this close, without the mark
            body = files.read_epoch(self.folder.read(path) or b"", self.identity.public()["sign"],
                                    self.store.store_id, n)
            if [d["device_id"] for d in body["devices"]] != [self.device_id] or body["checkpoint"]:
                raise
        listing = [p for p in self.folder.list() if p != path]
        for p in listing:
            self.folder.delete(p)
        if hasattr(self.folder, "refresh"):  # what the listing hadn't shown yet
            self.folder.refresh()
            late = [p for p in self.folder.list() if p != path and p not in listing]
            for p in late:
                self.folder.delete(p)
            listing += late
        with self.store.transaction():
            self.state.clear("cloud_keys", "cloud_read", "cloud_refused")
            self.state.leave(relays_only=True)
            self.state.unset("join", "last_join", "plan_digest", "epoch_pending", "missing_since", "starting", "error",
                             "closing", "previous_identity", "published_position", prefixes=["removing:", "rejoined:"])
            self.state.set("epoch", n)
            self.state.set("checkpoint_position", 0)
            self.state.set("closed", 1)
            self.state.set("off", 1)
        return len(listing)

    def _new_epoch_reason(self, listing: dict[str, str]) -> str | None:
        if self.store.last_erase() > int(self.state.get("checkpoint_position")):
            return "erase"
        if self.state.get("epoch_pending"):
            return self.state.get("epoch_pending")
        if sum(not p.startswith("checkpoints/") for p in listing) > MAX_FILES and not self._waiting():
            return "compact"
        return None

    def _newer(self) -> bool:
        return self.state.any_outcome(NEWER)

    def _waiting(self) -> list[str]:
        """Active devices that haven't written in the current epoch yet, so older epochs can't be deleted."""
        return [d["id"] for d in self.devices() if d["class"] == "relay" and d["moved_epoch"] < self.epoch]

    def _cleanup(self) -> dict:
        """Once every active device writes in the current epoch: delete every older file and key outright."""
        current = self.epoch
        old_keys = self.state.key_epochs_before(current)
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
        with self.store.transaction():
            self.state.drop_keys(before=current)
            self.state.forget(old)
            self.state.unset(prefixes=["rejoined:"])
        return {"cleaned": len(old)}

    def local_status(self) -> dict:
        """What this store knows about the folder, without asking Drive."""
        synced = self.state.get("synced_at")
        out: dict[str, Any] = {"store_id": self.store.store_id, "epoch": self.epoch,
                               "synced_at": _when(synced), "recovery": bool(self.state.get("recovery_key"))}
        if self.unshown_code():
            out["recovery_code"] = "not shown yet; run `byobot cloud rotate --recovery` for a new one"
        if self.is_off:
            out["off"] = True
        if self.state.get("error"):
            out["error"] = self.state.get("error")
        if self._newer():
            out["upgrade"] = "a device writes with a newer byobot; upgrade byobot on this Mac to read it"
        out["devices"] = [{**{k: d[k] for k in ("id", "class", "joined_epoch", "moved_epoch", "last_seq")},
                           "last_heard": _when(synced) if d["class"] == "keeper" else d["last_seen_at"]}
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


def _wire(row: dict) -> dict:
    """A devices row as the files name it."""
    return {"device_id": row["id"], "device_class": row["class"], "box": row["box"], "sign": row["sign"]}


def _when(stamp: str | None) -> str | None:
    return stamp and datetime.fromtimestamp(float(stamp)).astimezone().isoformat(timespec="seconds")


def due(store: Store, every: float, clock: Callable[[], float] = time.time) -> bool:
    """Whether a background sync is due: this store keeps a folder, Drive sync isn't off, and nothing tried in the
    last ``every`` seconds. A try stamped in the future (the clock ran ahead) counts as due. Reads only, so a store
    without Drive sync is left as it is."""
    meta = State(store.conn).meta(["identity", "off", "tried_at"])
    since = clock() - float(meta.get("tried_at", 0))
    return "identity" in meta and "off" not in meta and (since >= every or since < 0)


def tried(store: Store, clock: Callable[[], float] = time.time) -> None:
    State(store.conn).set("tried_at", clock())


def is_keeper(store: Store) -> bool:
    """Whether this store keeps a cloud folder."""
    return State(store.conn).get("identity") is not None


def _top_seq(listing: dict[str, str], device_id: str) -> int:
    """The keeper's next seq past every file of its own in the folder."""
    own = [segment.parse_name(p) for p in listing if p.startswith((f"segments/{device_id}/", "plan/"))]
    return max([w["seq"] for w in own if w and w["seq"] < SEQ_CEILING] + [0]) + 1


def _parts(events: list[dict]) -> list[list[dict]]:
    """Events cut into parts a reader takes: at most the hub's batch, and under the body limit encoded."""
    parts: list[list[dict]] = []
    size = 0
    for e in events:
        n = len(dumps(e)) + 1
        if not parts or len(parts[-1]) == rules.MAX_BATCH or size + n > segment.MAX_BODY - 4096:
            parts.append([])
            size = 0
        parts[-1].append(e)
        size += n
    return parts


def _closed(folder: Folder, listing: dict[str, str]) -> bool:
    """Whether `cloud off --delete` left this folder: nothing in it but epoch files, the newest marked closed."""
    epochs = sorted(p for p in listing if files.is_epoch_file(p))
    return bool(epochs) and len(epochs) == len(listing) and files.is_closed(folder.read(epochs[-1]))


def _recovery_epochs(listing: dict[str, str]) -> list[int]:
    return [e for p in listing if p.endswith(f"/{files.RECOVERY}.key") and (e := _epoch_of(p)) is not None]


def _epoch_of(path: str) -> int | None:
    """The epoch a cloud file belongs to; None for a device file."""
    found = segment.parse_name(path)
    if found:
        return found["epoch"]
    if files.epoch_number(path) is not None:
        return files.epoch_number(path)
    parts = path.split("/")
    if parts[0] == "keys" and len(parts) == 3 and parts[1].isdigit():
        return int(parts[1])
    return None


def _events(message: dict) -> list[dict]:
    body = message.get("events")
    events = body.get("events") if isinstance(body, dict) else None
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def apply(store: Store, writer: str, cls: str, messages: list[dict],
          rejected: Callable[[int, str], None] = lambda line, why: None) -> list[tuple[str, str]]:
    """Accept a verified segment's messages from its writer: a relay's through the hub's rules, the keeper's as
    they are. One (outcome, reason) per event, in order; a rejected samples message goes to ``rejected``."""
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
                    rejected(line, str(exc))
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
    return out
