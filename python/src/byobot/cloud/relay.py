"""A relay: a device that sends what it logged through the cloud folder and reads the keeper's plan.

This is the reference the phone's relay follows. It keeps what it sent until the keeper's receipts list the segment
that carried it, and sends again whatever was in a segment that left the folder unread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.relay import MAX_BATCH
from ..link import parse_pairing_link
from ..util import ByobotError
from . import files, segment
from .crypto import CloudError, Identity, join_proof, unb64
from .folder import Exists, Folder


def parse_join(link: str) -> dict:
    """The store, one-time secret and keeper's signing key in a ``byobot://pair`` link."""
    try:
        found = parse_pairing_link(link)
    except (ValueError, KeyError) as exc:
        raise ByobotError(f"that isn't a byobot join code this version understands ({exc})") from None
    if "store_id" not in found:
        raise ByobotError("that link pairs with a laptop; it holds no Drive join code")
    return {k: found[k] for k in ("store_id", "secret", "keeper_key")}


@dataclass
class Relay:
    folder: Folder
    store_id: str
    device_id: str
    keeper_sign: str
    identity: Identity = field(default_factory=Identity.generate)
    epoch: int | None = None
    key: bytes | None = None
    seq: int = 0
    events: dict[str, dict] = field(default_factory=dict)  # every event it wrote and the keeper hasn't read yet
    samples: list[dict] = field(default_factory=list)  # sample batches not yet in a segment
    sent: dict[str, dict] = field(default_factory=dict)  # unread segments -> {"events": ids, "samples": {line: batch}}
    rejected: list[dict] = field(default_factory=list)  # sample batches the keeper rejected, with why
    plan: dict | None = None
    receipts: dict = field(default_factory=dict)
    plan_path: str | None = None
    kind_versions: dict = field(default_factory=dict)
    removed: bool = False

    @classmethod
    def join(cls, folder: Folder, link: str, device_id: str, **kw: Any) -> Relay:
        code = parse_join(link)
        relay = cls(folder, code["store_id"], device_id, code["keeper_key"], **kw)
        public = relay.identity.public()
        proof = join_proof(unb64(code["secret"]), relay.store_id, device_id, public)
        if not relay._keeper_here(folder.list()):
            raise ByobotError("this Google account doesn't hold that Mac's logbook; sign in with the account the Mac "
                              "uses")
        path = files.device_path(device_id)
        data = files.device_file(relay.store_id, {"device_id": device_id, "device_class": "relay", **public}, proof)
        try:
            folder.create(path, data)
        except Exists:  # left by a join the keeper never finished; a device with a key is already in
            if any(p.startswith("keys/") and p.endswith(f"/{device_id}.key") for p in folder.list()):
                raise
            folder.delete(path)
            folder.create(path, data)
        return relay

    def _keeper_here(self, listing: dict[str, str]) -> bool:
        """Whether this folder holds an epoch file the code's keeper signed."""
        for p in sorted((p for p in listing if is_epoch_file(p)), reverse=True):
            try:
                files.read_epoch(self.folder.read(p) or b"", self.keeper_sign, self.store_id, files.epoch_number(p))
                return True
            except CloudError:
                continue
        return False

    def log(self, events: list[dict]) -> None:
        for e in events:
            self.events[e["id"]] = e

    def send_samples(self, body: dict) -> None:
        """Queue samples and deletions. A deletion of a sample not yet sent drops it; the Mac keeps any deletion that
        comes before its sample, so none waits."""
        self._drop({d["source_id"] for d in body.get("deleted", [])})
        self._queue(body)

    def in_flight(self) -> set[str]:
        """The source ids of samples in segments the keeper hasn't read yet."""
        return {s["source_id"] for c in self.sent.values() for b in c["samples"].values() for s in b["samples"]}

    def unread(self) -> set[str]:
        """The source ids of samples the keeper hasn't read yet, queued or sent. A phone with another route sends a
        deletion of one of these here, not there."""
        return self.in_flight() | {s["source_id"] for b in self.samples for s in b["samples"]}

    def _drop(self, gone: set[str]) -> None:
        for b in self.samples:
            b["samples"] = [s for s in b["samples"] if s["source_id"] not in gone]
        self.samples = [b for b in self.samples if b["samples"] or b["deleted"]]

    def _queue(self, body: dict) -> None:
        items = [("samples", s) for s in body.get("samples", [])] + [("deleted", d) for d in body.get("deleted", [])]
        if not items:
            return
        for i in range(0, len(items), MAX_BATCH):
            part = items[i:i + MAX_BATCH]
            self.samples.append({**body, "samples": [v for k, v in part if k == "samples"],
                                 "deleted": [v for k, v in part if k == "deleted"]})

    # -- sync ----------------------------------------------------------------

    def sync(self) -> dict:
        """Take a newer epoch's key, read the plan, then send what the keeper doesn't hold."""
        if self.removed:
            return {"removed": True}
        listing = self.folder.list()
        moved = self._adopt(listing)
        if self.removed:  # no keeper reads what it left in the folder now
            for path in listing:
                if path.startswith(f"segments/{self.device_id}/") or path == files.device_path(self.device_id):
                    try:
                        self.folder.delete(path)
                    except ByobotError:
                        pass
        if self.removed or self.key is None:
            return {"removed": self.removed, "joined": False}
        self._read_plan(listing)
        if self.plan is not None:
            self._apply_plan(self.plan)
        self._settle(listing)
        written = self._send(listing, force=moved)
        return {"epoch": self.epoch, "written": written, "pending": len(self.events)}

    def _follow(self, listing: dict[str, str]) -> dict[int, dict]:
        """The epoch files newer than this relay's, oldest first, each checked against the keeper key the one
        before it trusts: a handover moves that trust to the keeper's new key."""
        bodies, trusted = {}, self.keeper_sign
        for n in sorted(files.epoch_number(p) for p in listing if is_epoch_file(p)):
            if self.epoch is not None and n <= self.epoch:
                continue
            try:
                bodies[n] = files.read_epoch(self.folder.read(files.epoch_path(n)) or b"", trusted, self.store_id, n)
            except CloudError:
                continue
            trusted = bodies[n]["keeper_key"]
        return bodies

    def _adopt(self, listing: dict[str, str]) -> bool:
        epochs = sorted((files.epoch_number(p) for p in listing if is_epoch_file(p)), reverse=True)
        bodies = self._follow(listing)
        for n in sorted(bodies, reverse=True):
            body = bodies[n]
            key_data = self.folder.read(files.key_path(n, self.device_id))
            if key_data is None:
                if self.epoch is not None and all(d["device_id"] != self.device_id for d in body["devices"]):
                    self.removed, self.key = True, None
                return False
            try:
                key = files.open_key(key_data, self.identity, self.device_id, body["keeper_key"], self.store_id, n)
            except CloudError:
                continue
            first = self.epoch is None
            self.epoch, self.key, self.keeper_sign = n, key, body["keeper_key"]
            self.plan = self.plan_path = None
            self.receipts = {}
            return not first
        if self.epoch is None:  # admitted mid-epoch: no epoch file lists it yet, its key file does
            for n in epochs:
                key_data = self.folder.read(files.key_path(n, self.device_id))
                if key_data is None:
                    continue
                try:
                    self.key = files.open_key(key_data, self.identity, self.device_id, self.keeper_sign,
                                              self.store_id, n)
                    self.epoch = n
                    return False
                except CloudError:
                    continue
        return False

    def _read_plan(self, listing: dict[str, str]) -> None:
        plans = sorted((p for p in listing if p.startswith(f"plan/{files.epoch_dir(self.epoch)}-")), reverse=True)
        for path in plans:
            if self.plan_path is not None and path <= self.plan_path:
                return  # never step back to an older plan
            try:
                _, messages = segment.read(self.folder.read(path) or b"", path=path, store_id=self.store_id,
                                           key=self.key, sign_public=self.keeper_sign, skip_unknown=True)
            except CloudError:
                continue
            plan = next((m["plan"] for m in messages if "plan" in m), None)
            if plan is None:
                continue
            self.plan, self.plan_path = plan, path
            self.receipts = next((m["receipts"] for m in messages if "receipts" in m), {})
            return

    def _apply_plan(self, plan: dict) -> None:
        erased = {(r["kind"], r["id"]) for r in plan.get("erased", [])}
        for eid, e in list(self.events.items()):
            if (e["record"]["kind"], e["record"]["id"]) in erased:
                del self.events[eid]

    def _settle(self, listing: dict[str, str]) -> None:
        """Drop what a read segment carried; queue again the samples of one that left the folder unread."""
        mine = self.receipts.get(self.device_id) or {}
        read = set(mine.get("read", []))
        rejected = {(r["seq"], r["line"]): r["reason"] for r in mine.get("rejected", [])}
        for path, carried in list(self.sent.items()):
            seq = segment.parse_name(path)["seq"]
            if seq in read:
                for eid in carried["events"]:
                    self.events.pop(eid, None)
                self.rejected += [{"reason": rejected[(seq, line)], "samples": batch}
                                  for line, batch in carried["samples"].items() if (seq, line) in rejected]
                del self.sent[path]
            elif path not in listing:
                self.samples[:0] = list(carried["samples"].values())
                del self.sent[path]

    def _send(self, listing: dict[str, str], force: bool) -> int:
        in_folder = {i for carried in self.sent.values() for i in carried["events"]}
        todo = sorted((e for i, e in self.events.items() if i not in in_folder), key=lambda e: (e["hlc"], e["id"]))
        if not todo and not self.samples and not force:
            return 0
        written = 0
        while todo or self.samples or force:
            batch, todo = todo[:MAX_BATCH], todo[MAX_BATCH:]
            messages = [{"events": {"events": batch, **({"kind_versions": self.kind_versions} if self.kind_versions else {})}}] \
                if batch else []
            room = MAX_BATCH - len(batch)
            batches = []
            while self.samples and room >= segment.size([{"samples": self.samples[0]}]):
                room -= segment.size([{"samples": self.samples[0]}])
                batches.append(self.samples.pop(0))
            lines = {len(messages) + i: b for i, b in enumerate(batches)}
            messages += [{"samples": b} for b in batches]
            if not messages and not force:
                break
            self.seq += 1
            path = segment.name("segment", self.epoch, self.seq, self.device_id)
            try:
                self.folder.create(path, segment.write("segment", self.store_id, self.device_id, self.epoch, self.seq,
                                                       messages, self.key, self.identity))
            except BaseException:
                self.samples[:0] = batches  # its events are still in self.events; its samples go back in line
                raise
            self.sent[path] = {"events": [e["id"] for e in batch], "samples": lines}
            written, force = written + 1, False
        return written


def is_epoch_file(path: str) -> bool:
    return files.epoch_number(path) is not None
