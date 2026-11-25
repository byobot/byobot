"""The narrow interface every core stress test drives, whatever store sits behind it.

A driver turns design-neutral intents and source records into its own events, delivers
events between replicas and reports what an agent could observe (a canonical snapshot).
An operation a driver can't express raises Unsupported, which a scenario reports as
"no equivalent" rather than as a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

INTENTS = (
    "define_activity", "define_location", "define_plan", "plan_session", "replan_session", "log_session",
    "log_effort", "edit", "undo", "remove", "resolve", "correct_import", "same_as", "erase",
)
OUTCOMES = ("accepted", "duplicate", "stored_unprojected", "rejected")
SOURCES = ("whoop", "apple-health", "hume")


class Unsupported(Exception):
    """The driver has no way to express this operation."""


@dataclass
class Clock:
    """A device clock: true time plus a skew, written in the device's UTC offset."""

    skew: timedelta = timedelta(0)
    offset: timedelta = timedelta(0)
    frozen: datetime | None = None

    def read(self, true_now: datetime) -> str:
        at = self.frozen or (true_now + self.skew)
        return stamp(at, self.offset)


def stamp(at: datetime, offset: timedelta = timedelta(0)) -> str:
    local = at.astimezone(timezone(offset))
    text = local.isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass
class Replica:
    """A store on one device. `state` belongs to the driver."""

    name: str
    clock: Clock
    state: Any = None
    held: set[str] = field(default_factory=set)  # harness change ids this replica has


class Driver(Protocol):
    name: str

    def open(self, name: str, directory: Any) -> Any: ...

    def close(self, state: Any) -> None: ...

    def author(self, state: Any, intent: dict, at: str, actor: dict) -> list[Any]:
        """Apply an intent on this replica. Returns the events it produced."""

    def deliver(self, state: Any, events: list[Any]) -> list[tuple[str, str]]:
        """Sync events in. One (outcome, reason) per event."""

    def ingest(self, state: Any, source: str, batch: Any, at: str) -> list[Any]:
        """A connector delivering source records in its wire shape. Returns the events it produced."""

    def log_events(self, state: Any) -> list[Any]:
        """Every event this replica holds, in arrival order, ready to deliver elsewhere."""

    def rebuild(self, state: Any) -> None: ...

    def snapshot(self, state: Any) -> dict:
        """The canonical observable state (see snapshot.py)."""

    def occurrences(self, state: Any) -> list[dict]:
        """Workouts as reads see them, after merging sources: start, end, normalized metrics."""

    def source_history(self, state: Any, source: str, source_id: str) -> list[dict]:
        """Every revision of one source record still readable, oldest first."""

    def log_digest(self, state: Any) -> list[str]:
        """The authored log, without ids or arrival order, for comparing histories."""

    def local_dates(self, state: Any) -> dict[str, str]:
        """Each session and effort's local date, as reads bucket it."""

    def read(self, state: Any, surface: str, command: str, args: dict) -> Any: ...

    def export_log(self, state: Any) -> Any: ...

    def restore(self, name: str, directory: Any, log: Any) -> Any: ...

    def upgrade(self, state: Any, version: int) -> None: ...
