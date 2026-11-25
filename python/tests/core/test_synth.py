"""The synthetic persona: deterministic, realistic in shape, and accepted by every driver."""

import hashlib
import statistics
from collections import Counter

from byobot import whoop
from byobot.whoop import ENDPOINTS as WHOOP_PATHS

from .harness import World
from .model import Reference
from .snapshot import diff
from .synth import persona, play

YEAR = persona(1, 365)


def settle(world: World) -> list[str]:
    live = [n for n in world.replicas if n != "phone"]
    for a in live:
        for b in live:
            if a != b:
                world.sync(a, b)
    return live


def digest(p) -> str:
    return hashlib.sha256(repr(p.steps).encode()).hexdigest()


def test_same_seed_same_bytes():
    assert digest(persona(7, 60)) == digest(persona(7, 60))
    assert digest(persona(7, 60)) != digest(persona(8, 60))


def test_whoop_batches_match_the_pinned_api():
    batches = [s.args[1] for s in YEAR.steps if s.op == "ingest" and s.args[0] == "whoop"]
    for batch in batches:
        for kind, records in batch.items():
            whoop.check({"records": records}, WHOOP_PATHS[kind][1])
    assert sum(len(b.get("workout", [])) for b in batches) > 100


def test_the_year_has_travel_daylight_time_skew_and_a_new_phone():
    offsets = {s.args[0]["record"]["started_at"][-6:] for s in YEAR.steps
               if s.op == "author" and s.args[0]["intent"] == "log_session"}
    assert {"-05:00", "-04:00"} <= offsets and len(offsets) > 2
    assert any(s.op == "clock" and s.args[0] for s in YEAR.steps)
    assert [s.replica for s in YEAR.steps if s.op == "device"] == ["phone2"]
    syncs = [s.at for s in YEAR.steps if s.op == "sync" and s.replica.startswith("phone")]
    assert max((b - a).days for a, b in zip(syncs, syncs[1:])) >= 25
    assert YEAR.revised


def test_training_volume():
    sets = Counter(s.args[0]["record"]["session_id"] for s in YEAR.steps
                   if s.op == "author" and s.args[0]["intent"] == "log_effort")
    assert 180 <= len(sets) <= 215  # four strength days a week
    assert 18 <= statistics.median(sets.values()) <= 30
    assert any(s.op == "undo" for s in YEAR.steps)
    assert any(s.args[0]["intent"] == "replan_session" for s in YEAR.steps if s.op == "author")


def test_a_fortnight_lands_whole(world):
    play(world, persona(5, 14))
    live = settle(world)
    assert not [c.id for c in world.changes.values() if c.refused]
    for name in live:
        assert diff(world.snapshot(name), world.expected(name)) == []


def test_every_workout_counts_once(tmp_path):
    p = persona(2, 120)
    world = World(Reference(), tmp_path)
    play(world, p)
    settle(world)
    assert len(Reference().occurrences(world.state("laptop"))) == len(p.workouts)
