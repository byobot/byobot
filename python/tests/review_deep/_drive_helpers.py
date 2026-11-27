"""Helpers for the deep Drive sync review: a shared folder whose listings lag per reader, with faults, and a
randomized keeper-and-relays simulation checked against a reference model."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from byobot.cloud import segment  # noqa: E402
from byobot.cloud.crypto import CloudError  # noqa: E402
from byobot.cloud.folder import Exists, checksum  # noqa: E402
from byobot.cloud.keeper import Keeper  # noqa: E402
from byobot.cloud.relay import Relay  # noqa: E402
from byobot.core import commands, hlc  # noqa: E402
from byobot.util import ByobotError, uuid7  # noqa: E402

ME = {"kind": "human", "id": "me"}
SECRET = "zq-deep-erase-91c2"
PHONES = ["0193a0b0-0000-7000-8000-00000000dd01", "0193a0b0-0000-7000-8000-00000000dd02",
          "0193a0b0-0000-7000-8000-00000000dd03"]


class Flaky(ByobotError):
    """Drive failed this call (busy, 5xx, network)."""


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Shared:
    """What Drive holds: path -> (bytes, when it landed). Each reader sees it through a View that lags."""

    def __init__(self, rng: random.Random | None = None, max_lag: int = 0):
        self.files: dict[str, bytes] = {}
        self.born: dict[str, int] = {}
        self.gone: dict[str, tuple[int, str]] = {}  # path -> (when deleted, checksum it had)
        self.lag: dict[str, int] = {}
        self.tick = 0
        self.rng = rng or random.Random(0)
        self.max_lag = max_lag
        self.fault_rate = 0.0

    def view(self, who: str) -> View:
        return View(self, who)

    def settle(self) -> None:
        """Every listing catches up and faults stop."""
        self.max_lag = 0
        self.fault_rate = 0.0
        self.lag = {p: 0 for p in self.lag}
        self.tick += 10


class View:
    """One reader's folder: its own writes show at once, others' after their lag; deletions likewise."""

    def __init__(self, shared: Shared, who: str):
        self.s, self.who = shared, who
        self.mine: set[str] = set()
        self.fresh: set[str] = set()  # what a full listing showed: there, or gone

    def _fault(self, what: str) -> None:
        if self.s.fault_rate and self.s.rng.random() < self.s.fault_rate:
            raise Flaky(f"Drive failed a {what}")

    def list(self) -> dict[str, str]:
        self._fault("listing")
        s = self.s
        out = {}
        for p, d in s.files.items():
            if p in self.mine or p in self.fresh or s.tick >= s.born[p] + s.lag.get(p, 0):
                out[p] = checksum(d)
        for p, (when, sum_) in s.gone.items():
            if p not in s.files and p not in self.mine and p not in self.fresh and s.tick < when + s.lag.get(p, 0):
                out[p] = sum_
        return dict(sorted(out.items()))

    def refresh(self) -> None:
        self.fresh = set(self.s.files) | set(self.s.gone)

    def read(self, path: str) -> bytes | None:
        self._fault("read")
        return self.s.files.get(path)

    def create(self, path: str, data: bytes) -> None:
        self._fault("upload")
        s = self.s
        if path in s.files:
            raise Exists(path)
        s.files[path] = bytes(data)
        s.born[path] = s.tick
        s.lag[path] = s.rng.randint(0, s.max_lag) if s.max_lag else 0
        s.gone.pop(path, None)
        self.mine.add(path)
        if s.fault_rate and s.rng.random() < s.fault_rate / 2:
            raise Flaky(f"the answer to {path}'s upload was lost")

    def delete(self, path: str) -> None:
        self._fault("delete")
        s = self.s
        if path in s.files:
            s.gone[path] = (s.tick, checksum(s.files.pop(path)))
            s.lag[path] = s.rng.randint(0, s.max_lag) if s.max_lag else 0
        self.mine.discard(path)


def effort(device_id: str, n: int, kind: str = "effort") -> dict:
    return {"id": uuid7(), "schema_version": 1, "record": {"kind": kind, "id": uuid7()}, "op": "put",
            "kind_version": 1, "hlc": hlc.format((1795000000000 + 1000 * n, 0)), "ts": "2026-11-23T08:00:00-05:00",
            "device_id": device_id, "actor": ME, "prev": [],
            "payload": {"activity_id": "barbell_back_squat", "modality": "self_reported",
                        "time": {"start": "2026-11-23T07:40:00-05:00"}}}


def sample(source_id: str, value: float = 80.0) -> dict:
    return {"source": "apple_health", "source_id": source_id, "type": "HKQuantityTypeIdentifierBodyMass",
            "start": "2026-11-23T06:30:00-05:00", "end": "2026-11-23T06:30:00-05:00", "value": value, "unit": "kg"}


def deletion(source_id: str) -> dict:
    return {"source": "apple_health", "source_id": source_id}


def live_samples(store) -> set[str]:
    return {r["payload"]["source_id"] for r in store.records("source_record")}


def opened(folder_files: dict[str, bytes], keeper: Keeper, key: bytes) -> list[str]:
    """Every segment the key opens, as text."""
    out = []
    for path, data in folder_files.items():
        where = segment.parse_name(path)
        if where is None:
            continue
        writer = where.get("device_id") or keeper.device_id
        device = keeper.device(writer) or {"sign": keeper.identity.public()["sign"]}
        try:
            out.append(json.dumps(segment.read(data, path=path, store_id=keeper.store.store_id, key=key,
                                               sign_public=device["sign"])[1]))
        except CloudError:
            pass
    return out


def tolerant(call, *args, **kw):
    """Run a sync step; a Drive failure is what a real run would log and retry next time."""
    try:
        return call(*args, **kw)
    except (Flaky, ByobotError) as exc:
        return {"failed": str(exc)}


class Sim:
    """A keeper and some relays over one lagging, faulty folder, with what each phone meant as the reference."""

    def __init__(self, store, seed: int, relays: int = 2, max_lag: int = 2, fault_rate: float = 0.1,
                 erase: bool = True, compact: bool = True, extra: tuple = (), workdir=None):
        self.rng = random.Random(seed)
        self.shared = Shared(random.Random(seed + 1), 0)
        self.clock = Clock()
        self.store = store
        self.keeper = Keeper.setup(store, self.shared.view("mac"), self.clock, recovery=True)
        self.code = self.keeper.recovery_code
        self.relays: list[Relay] = []
        for i in range(relays):
            link = self.keeper.join_code()["link"]
            relay = Relay.join(self.shared.view(f"phone{i}"), link, PHONES[i])
            for _ in range(20):
                self.shared.tick += 1
                self.keeper.sync()
                relay.sync()
                if relay.key is not None:
                    break
            assert relay.key is not None
            self.relays.append(relay)
        self.shared.fault_rate, self.shared.max_lag = fault_rate, max_lag
        self.erase, self.compact = erase, compact
        self.logged: set[str] = set()
        self.samples_live: set[str] = set()
        self.owner: dict[str, int] = {}
        self.samples_all: set[str] = set()
        self.n = 0
        self.secrets: list[str] = []
        self.trace: list[str] = []
        self.extra, self.workdir = extra, workdir
        self.spare = list(PHONES[relays:]) + [f"0193a0b0-0000-7000-8000-0000000e{k:04d}" for k in range(50)]
        self.macs = 0
        self.lost_ok: set[str] = set()

    def _admit(self, relay: Relay) -> bool:
        for _ in range(3):
            tolerant(self.keeper.sync)
            tolerant(relay.sync)
            if relay.key is not None:
                return True
        return False

    def rejoin(self, relay: Relay) -> Relay:
        """A phone still waiting to be let in scans a new code, as a person would."""
        try:
            new = Relay.join(relay.folder, self.keeper.join_code()["link"], relay.device_id,
                             identity=relay.identity)
        except (Flaky, ByobotError, Exists):
            return relay
        new.events, new.samples, new.held = relay.events, relay.samples, relay.held
        self._admit(new)
        return new

    def replace_phone(self, i: int) -> None:
        """`cloud remove` the phone, which joins again as a new install and sends what it still holds."""
        old = self.relays[i]
        if old.key is None:
            self.relays[i] = self.rejoin(old)
            return
        tolerant(self.keeper.remove, old.device_id)
        if not (self.keeper.device(old.device_id) or {}).get("removed"):
            return
        device = self.spare.pop(0)
        new = Relay(self.shared.view(f"phone-{device}"), old.store_id, device, old.keeper_sign)
        self.lost_ok |= set(old.events)  # a removed phone's unread events stay on it, as `cloud remove` says
        left = [*old.samples, *(b for c in old.sent.values() for b in c["samples"].values())]
        for b in left:
            new.send_samples({"samples": b["samples"], "deleted": b["deleted"]})
        if old.held:
            new.send_samples({"samples": [], "deleted": old.held})
        self.relays[i] = self.rejoin(new)
        self.trace.append(f"replace {i} -> {device} admitted={self.relays[i].key is not None}")

    def handover(self) -> None:
        """The Mac is lost: a new one recovers from the folder with the code and carries on."""
        from byobot.core.store import Store

        self.macs += 1
        fresh = Store(Path(self.workdir) / f"mac{self.macs}.db")
        try:
            keeper = Keeper.recover(fresh, self.shared.view(f"mac{self.macs}"), self.code, self.clock)
        except (Flaky, ByobotError) as exc:
            fresh.close()
            self.trace.append(f"handover failed {exc}")
            return
        self.store, self.keeper = fresh, keeper
        self.secrets = [r for r in self.secrets if fresh.record("location", r)]
        self.trace.append(f"handover to mac{self.macs} epoch {keeper.epoch}")

    def step(self) -> None:
        rng = self.rng
        r = rng.random()
        i = rng.randrange(len(self.relays))
        relay = self.relays[i]
        self.n += 1
        if relay.key is None and not relay.removed and r < 0.1:
            self.relays[i] = self.rejoin(relay)
        elif r < 0.2:
            events = [effort(relay.device_id, self.n * 10 + k) for k in range(rng.randint(1, 3))]
            relay.log(events)
            self.logged |= {e["id"] for e in events}
            self.trace.append(f"log {i} {len(events)}")
        elif r < 0.3:
            sid = f"S{self.n}"
            relay.send_samples({"samples": [sample(sid)]})
            self.samples_live.add(sid)
            self.owner[sid] = i
            self.samples_all.add(sid)
            self.trace.append(f"sample {i} {sid}")
        elif r < 0.35 and any(self.owner[s] == i for s in self.samples_live):
            sid = rng.choice(sorted(s for s in self.samples_live if self.owner[s] == i))
            relay.send_samples({"samples": [], "deleted": [deletion(sid)]})
            self.samples_live.discard(sid)
            self.trace.append(f"delete {i} {sid}")
        elif r < 0.6:
            self.trace.append(f"relay sync {i} {tolerant(relay.sync)}")
        elif r < 0.85:
            self.trace.append(f"keeper sync {tolerant(self.keeper.sync)}")
        elif r < 0.9:
            commands.log(self.store, ME, [{"kind": "location", "id": f"place-{self.n}",
                                           "payload": {"name": f"{SECRET}-{self.n}-x" if self.erase else "Gym"}}])
            self.secrets.append(f"place-{self.n}")
            self.trace.append(f"mac log place-{self.n}")
        elif r < 0.93 and self.erase and self.secrets:
            rid = self.secrets.pop(0)
            commands.erase(self.store, ME, "location", rid)
            self.trace.append(f"erase {rid}")
        elif r < 0.95 and self.compact:
            self.trace.append(f"compact {tolerant(self.keeper.compact)}")
        elif "other" in self.extra and r < 0.965:
            from byobot.core.store import Store

            other = Keeper(Store(self.store.path), self.shared.view(f"other{self.n}"), self.clock)
            self.trace.append(f"other process sync {tolerant(other.sync)}")
        elif "restart" in self.extra and r < 0.975:
            self.keeper = Keeper(self.store, self.shared.view(f"restart{self.n}"), self.clock)
            self.trace.append("keeper restarted")
        elif "replace" in self.extra and r < 0.985:
            self.replace_phone(i)
        elif "handover" in self.extra and r < 0.99:
            self.handover()
        else:
            self.shared.tick += 1
            self.clock.now += 10
        if rng.random() < 0.3:
            self.shared.tick += 1
            self.clock.now += 10

    def settle(self, rounds: int = 8) -> None:
        self.shared.settle()
        for i, relay in enumerate(self.relays):
            if relay.key is None:
                self.relays[i] = self.rejoin(relay)
        for _ in range(rounds):
            self.clock.now += 40
            self.shared.tick += 1
            for relay in self.relays:
                relay.sync()
            self.keeper.sync()

    def run(self, steps: int) -> None:
        for _ in range(steps):
            self.step()
        self.settle()


def joined(keeper: Keeper, folder, device_id: str = PHONES[0]) -> Relay:
    """A phone that joined with a code and took the current key."""
    relay = Relay.join(folder, keeper.join_code()["link"], device_id)
    assert keeper.sync()["join"] == "admitted"
    relay.sync()
    assert relay.epoch == keeper.epoch and relay.key is not None
    return relay
