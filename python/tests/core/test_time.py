"""S11: travel and daylight time. Every read runs under four machine time zones."""

import os
import time
from contextlib import contextmanager

import pytest

from .scenarios import effort, session, whoop_workout
from .synth import persona, play

ZONES = ("America/New_York", "Europe/London", "Asia/Tokyo", "UTC")
pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")


@contextmanager
def machine_zone(zone: str):
    before = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_s11_local_dates_come_from_the_written_offset(world):
    world.replica("phone")
    world.author("phone", effort("e1", at="2026-11-21T00:30:00.000+05:00"))
    world.author("phone", effort("e2", at="2026-11-20T21:00:00.000Z"))
    world.author("phone", session("s1", "2026-03-02T23:30:00.000-05:00", "2026-03-03T00:40:00.000-05:00"))
    want = {"effort/e1": "2026-11-21", "effort/e2": "2026-11-20", "session/s1": "2026-03-02"}
    for zone in ZONES:
        with machine_zone(zone):
            assert world.local_dates("phone") == want, zone


def test_s11_a_fortnight_abroad_dates_every_set_where_it_happened(world):
    p = persona(3, 14)
    play(world, p)
    for zone in ZONES:
        with machine_zone(zone):
            dates = world.local_dates("phone")
            assert {k: v for k, v in dates.items() if k in p.local_dates} == \
                {k: p.local_dates[k] for k in dates if k in p.local_dates}, zone


@pytest.mark.parametrize("start, end, seconds", [
    pytest.param("2026-03-08T01:30:00.000-05:00", "2026-03-08T03:30:00.000-04:00", "3600", id="spring_forward"),
    pytest.param("2026-11-01T01:10:00.000-04:00", "2026-11-01T01:20:00.000-05:00", "4200", id="fall_back"),
])
def test_s11_a_workout_across_a_clock_change_lasts_its_real_length(world, start, end, seconds):
    world.replica("laptop")
    w = whoop_workout("9a1b0000-0000-4000-8000-000000000005", start, end, "2026-11-02T08:00:00.000Z")
    world.ingest("laptop", "whoop", {"workout": [{**w, "timezone_offset": end[-6:]}]})
    [one] = world.occurrences("laptop")
    assert one["metrics"]["duration_s"] == seconds
    assert world.occurrences("laptop") == world.expected_occurrences("laptop")
