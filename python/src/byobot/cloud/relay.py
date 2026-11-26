"""A relay: a device that sends what it logged through the cloud folder and reads the keeper's plan.

This is the reference the phone's relay follows. It keeps what it sent until the keeper's receipts list the segment
that carried it, and sends again whatever was in a segment that left the folder unread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..core.relay import MAX_BATCH
from ..util import ByobotError
from . import files, segment
from .crypto import CloudError, Identity, join_proof, unb64
from .folder import Folder


def parse_join(link: str) -> dict:
    """The store, one-time secret and keeper's signing key in a ``byobot://cloud-join`` link."""
    parts = urlsplit(link)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    if parts.scheme != "byobot" or parts.netloc != "cloud-join" or not {"store", "secret", "keeper"} <= set(query):
        raise ByobotError("that isn't a byobot cloud join code")
    return {"store_id": query["store"], "secret": query["secret"], "keeper": query["keeper"]}


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
    refused: list[dict] = field(default_factory=list)  # sample batches the keeper refused, with why
    plan: dict | None = None
    receipts: dict = field(default_factory=dict)
    plan_path: str | None = None
    kinds: dict = field(default_factory=dict)
    removed: bool = False

    @classmethod
    def join(cls, folder: Folder, link: str, device_id: str, **kw: Any) -> Relay:
        code = parse_join(link)
        relay = cls(folder, code["store_id"], device_id, code["keeper"], **kw)
        public = relay.identity.public()
        proof = join_proof(unb64(code["secret"]), relay.store_id, device_id, public)
        folder.create(files.device_path(device_id),
                      files.device_file(relay.store_id, {"id": device_id, "class": "relay", **public}, proof))
        return relay

    def log(self, events: list[dict]) -> None:
        for e in events:
            self.events[e["id"]] = e

    def send_samples(self, body: dict) -> None:
        items = [("samples", s) for s in body.get("samples", [])] + [("deleted", d) for d in body.get("deleted", [])]
        for i in range(0, max(len(items), 1), MAX_BATCH):
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
        if self.removed or self.key is None:
            return {"removed": self.removed, "joined": False}
        self._read_plan(listing)
        if self.plan is not None:
            self._apply_plan(self.plan)
        self._settle(listing)
        written = self._send(listing, force=moved)
        return {"epoch": self.epoch, "written": written, "pending": len(self.events)}

    def _adopt(self, listing: dict[str, str]) -> bool:
        epochs = sorted((int(p[7:11]) for p in listing if is_epoch_file(p)), reverse=True)
        for n in epochs:
            if self.epoch is not None and n <= self.epoch:
                break
            data = self.folder.read(files.epoch_path(n))
            try:
                body = files.read_epoch(data or b"", self.keeper_sign, self.store_id, n)
            except CloudError:
                continue
            key_data = self.folder.read(files.key_path(n, self.device_id))
            if key_data is None:
                if self.epoch is not None and all(d["id"] != self.device_id for d in body["devices"]):
                    self.removed, self.key = True, None
                return False
            try:
                key = files.open_key(key_data, self.identity, self.device_id, self.keeper_sign, self.store_id, n)
            except CloudError:
                continue
            first = self.epoch is None
            self._requeue()
            self.epoch, self.key = n, key
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
        plans = sorted((p for p in listing if p.startswith(f"plan/{self.epoch:04d}-")), reverse=True)
        for path in plans:
            if self.plan_path is not None and path <= self.plan_path:
                return  # never step back to an older plan
            try:
                _, messages = segment.read(self.folder.read(path) or b"", path=path, store_id=self.store_id,
                                           key=self.key, sign_public=self.keeper_sign)
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
        refused = {(r["seq"], r["line"]): r["reason"] for r in mine.get("refused", [])}
        for path, carried in list(self.sent.items()):
            seq = segment.parse_name(path)["seq"]
            if seq in read:
                for eid in carried["events"]:
                    self.events.pop(eid, None)
                self.refused += [{"reason": refused[(seq, line)], "samples": batch}
                                 for line, batch in carried["samples"].items() if (seq, line) in refused]
                del self.sent[path]
            elif path not in listing:
                self.samples[:0] = list(carried["samples"].values())
                del self.sent[path]

    def _requeue(self) -> None:
        """Moving to a new epoch: send again whatever no receipt covered."""
        self.samples[:0] = [b for carried in self.sent.values() for b in carried["samples"].values()]
        self.sent = {}

    def _send(self, listing: dict[str, str], force: bool) -> int:
        in_folder = {i for carried in self.sent.values() for i in carried["events"]}
        todo = sorted((e for i, e in self.events.items() if i not in in_folder), key=lambda e: (e["hlc"], e["id"]))
        if not todo and not self.samples and not force:
            return 0
        written = 0
        while todo or self.samples or force:
            batch, todo = todo[:MAX_BATCH], todo[MAX_BATCH:]
            messages = [{"events": {"events": batch, **({"kinds": self.kinds} if self.kinds else {})}}] \
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
            self.folder.create(path, segment.write("segment", self.store_id, self.device_id, self.epoch, self.seq,
                                                   messages, self.key, self.identity))
            self.sent[path] = {"events": [e["id"] for e in batch], "samples": lines}
            written, force = written + 1, False
        return written


def is_epoch_file(path: str) -> bool:
    return path.startswith("epochs/") and path.endswith(".json") and path[7:11].isdigit() and len(path) == 16
