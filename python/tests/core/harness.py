"""A world of replicas driven through one driver, with the reference model shadowing every step.

Each replica has a shadow model replica that sees the same intents and deliveries, so
`expected(name)` is what the decided semantics say that replica should show.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from byobot.util import ByobotError

from .driver import Clock, Replica, Unsupported, parse
from .model import ModelReplica, Reference, Refused, Write

HUMAN = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "example-agent"}
START = "2026-03-02T09:00:00Z"


@dataclass(frozen=True)
class Change:
    """One intent or import on one replica: the driver's events and the model's writes for it."""

    id: str
    replica: str
    events: tuple
    writes: tuple[Write, ...]
    refused: bool = False


class World:
    def __init__(self, driver: Any, directory: Path, start: str = START):
        self.driver, self.directory = driver, Path(directory)
        self.now = parse(start)
        self.replicas: dict[str, Replica] = {}
        self.shadows: dict[str, ModelReplica] = {}
        self.changes: dict[str, Change] = {}

    def close(self) -> None:
        for rep in self.replicas.values():
            self.driver.close(rep.state)

    # -- devices -------------------------------------------------------------

    def replica(self, name: str, skew: timedelta = timedelta(0), offset: timedelta = timedelta(0)) -> Replica:
        rep = Replica(name, Clock(skew, offset), self.driver.open(name, self.directory))
        self.replicas[name], self.shadows[name] = rep, ModelReplica(name)
        return rep

    def restore(self, name: str, backup: tuple, skew: timedelta = timedelta(0)) -> Replica:
        """A new install restored from `backup`, an earlier `export`."""
        log, model_log, held = backup
        if name in self.replicas:  # the same device, wiped and restored
            self.driver.close(self.replicas[name].state)
        directory = self.directory / f"restore-{len(self.changes)}"
        directory.mkdir()
        rep = Replica(name, Clock(skew), self.driver.restore(name, directory, log), set(held))
        self.replicas[name], self.shadows[name] = rep, Reference().restore(name, self.directory, model_log)
        return rep

    def export(self, name: str) -> tuple:
        rep = self.replicas[name]
        return self.driver.export_log(rep.state), Reference().export_log(self.shadows[name]), set(rep.held)

    def tick(self, seconds: float = 60) -> None:
        self.now += timedelta(seconds=seconds)

    def clock(self, name: str) -> Clock:
        return self.replicas[name].clock

    # -- writes --------------------------------------------------------------

    def _change(self, name: str, events: list, writes: list, refused: bool = False) -> Change:
        change = Change(f"c{len(self.changes) + 1}", name, tuple(events), tuple(writes), refused)
        self.changes[change.id] = change
        self.replicas[name].held.add(change.id)
        return change

    def author(self, name: str, intent: dict, actor: dict = HUMAN) -> Change:
        """Apply an intent on a replica. A refusal by either side is recorded, not raised."""
        rep, shadow = self.replicas[name], self.shadows[name]
        at = rep.clock.read(self.now)
        self.tick(1)
        native, model = intent, intent
        if intent["intent"] == "undo":
            target = self.changes[intent["change"]]
            if not target.events and not target.writes:
                return self._change(name, [], [], refused=True)
            if not target.events:
                raise Unsupported("the driver wrote no event to undo")
            native = {"intent": "undo", "event": target.events[-1], "events": list(target.events)}
            model = {"intent": "undo", "events": list(target.writes)}
        try:
            writes = shadow.author(model, at, f"{actor['kind']}:{actor['id']}")
        except Refused:
            writes = []
        try:
            events = self.driver.author(rep.state, native, at, actor)
        except (ByobotError, Refused):
            events = []
        assert bool(events) == bool(writes), f"the store and the model disagree on refusing {intent}"
        return self._change(name, events, writes, refused=not writes)

    def ingest(self, name: str, source: str, batch: Any) -> Change:
        rep = self.replicas[name]
        at = rep.clock.read(self.now)
        self.tick(1)
        writes = self.shadows[name].ingest(source, batch, at)
        events = self.driver.ingest(rep.state, source, batch, at)
        puts = [e["id"] for e in events if isinstance(e, dict) and e["op"] == "put"]
        if puts and len(puts) == len([w for w in writes if w.op == "revision"]):  # a store's ids order revisions
            order = iter(puts)
            writes = [self.shadows[name].stamp(w.id, next(order)) if w.op == "revision" else w for w in writes]
        return self._change(name, events, writes)

    def newer(self, cid: str, variant: str) -> Change:
        """The change as an install one version newer would have written it."""
        c = self.changes[cid]
        change = self._change(c.replica, self.driver.newer(list(c.events), variant),
                              Reference().newer(list(c.writes), variant))
        self.replicas[c.replica].held.discard(change.id)  # held only where it's delivered
        return change

    def upgrade(self, name: str, version: int) -> None:
        self.driver.upgrade(self.replicas[name].state, version)
        self.shadows[name].version = version

    # -- sync ----------------------------------------------------------------

    def deliver(self, name: str, changes: list[Change]) -> list[tuple[str, str]]:
        rep = self.replicas[name]
        outcomes = self.driver.deliver(rep.state, [e for c in changes for e in c.events])
        self.shadows[name].deliver([w for c in changes for w in c.writes])
        rep.held.update(c.id for c in changes)
        return outcomes

    def missing(self, src: str, dst: str) -> list[Change]:
        have = self.replicas[dst].held
        return [c for c in self.ordered(self.replicas[src].held) if c.id not in have]

    def sync(self, src: str, dst: str) -> list[tuple[str, str]]:
        return self.deliver(dst, self.missing(src, dst))

    def ordered(self, ids: set[str]) -> list[Change]:
        return sorted((self.changes[i] for i in ids), key=lambda c: int(c.id[1:]))

    # -- reads ---------------------------------------------------------------

    def snapshot(self, name: str) -> dict:
        return self.driver.snapshot(self.replicas[name].state)

    def expected(self, name: str) -> dict:
        return self.shadows[name].snapshot()

    def occurrences(self, name: str) -> list[dict]:
        return self.driver.occurrences(self.replicas[name].state)

    def expected_occurrences(self, name: str) -> list[dict]:
        return Reference().occurrences(self.shadows[name])

    def source_history(self, name: str, source: str, source_id: str) -> list[dict]:
        return self.driver.source_history(self.replicas[name].state, source, source_id)

    def log_digest(self, name: str) -> list[str]:
        return self.driver.log_digest(self.replicas[name].state)

    def local_dates(self, name: str) -> dict[str, str]:
        return self.driver.local_dates(self.replicas[name].state)

    def rebuild(self, name: str) -> None:
        self.driver.rebuild(self.replicas[name].state)

    def state(self, name: str) -> Any:
        return self.replicas[name].state


def day(n: int, hour: int = 10, minute: int = 0, offset: timedelta = timedelta(0)) -> str:
    base = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(days=n, hours=hour, minutes=minute)
    return base.replace(tzinfo=timezone(offset)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
