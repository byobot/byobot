"""Hybrid logical clocks: `ms-counter` readings that only move forward and never fall behind a seen event."""

from __future__ import annotations

from datetime import datetime

DIGITS = (13, 6)
RUNAWAY_MS = 24 * 3600 * 1000  # a device more than a day ahead of this one is flagged


def parse(text: str) -> tuple[int, int]:
    ms, counter = text.split("-")
    return int(ms), int(counter)


def format(reading: tuple[int, int]) -> str:
    ms, counter = reading
    if ms >= 10 ** DIGITS[0] or counter >= 10 ** DIGITS[1]:
        raise OverflowError(f"hlc out of range: {reading}")
    return f"{ms:013d}-{counter:06d}"


def millis(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def tick(floor: tuple[int, int], wall_ms: int) -> tuple[int, int]:
    """The next reading after `floor` at wall time `wall_ms`."""
    return (wall_ms, 0) if wall_ms > floor[0] else (floor[0], floor[1] + 1)


def runaway(seen: tuple[int, int], wall_ms: int) -> bool:
    return seen[0] - wall_ms > RUNAWAY_MS
