"""I1 to I5 on generated histories: offline edits, undos and removes on several devices, any delivery order."""

import random
import tempfile
from datetime import timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from .conftest import DRIVERS
from .harness import World
from .snapshot import diff, equal

REPLICAS = ("laptop", "phone", "tablet")
IDS = ("e1", "e2")

step = st.one_of(
    st.tuples(st.just("log"), st.integers(0, 2), st.sampled_from(IDS), st.integers(1, 9)),
    st.tuples(st.just("edit"), st.integers(0, 2), st.sampled_from(IDS), st.integers(1, 9)),
    st.tuples(st.just("undo"), st.integers(0, 2), st.integers(0, 50)),
    st.tuples(st.just("remove"), st.integers(0, 2), st.sampled_from(IDS)),
    st.tuples(st.just("sync"), st.integers(0, 2), st.integers(0, 2)),
    st.tuples(st.just("skew"), st.integers(0, 2), st.integers(-48, 48)),
)
programs = st.lists(step, min_size=1, max_size=14)

# An undo synced before the event it undoes: v0 rejects it for good.
UNDO_FIRST = [("log", 0, "e1", 5), ("sync", 0, 1), ("edit", 1, "e1", 9), ("undo", 1, 1), ("sync", 1, 2)]
# The phone, three hours fast, edits first; the laptop edits later having seen it.
FAST_CLOCK = [("log", 0, "e1", 5), ("sync", 0, 1), ("skew", 1, 3), ("edit", 1, "e1", 5), ("sync", 1, 0),
              ("edit", 0, "e1", 9)]
# Two devices edit one set while apart.
APART = [("log", 0, "e1", 5), ("sync", 0, 1), ("edit", 0, "e1", 6), ("edit", 1, "e1", 9)]


def effort(rid: str, reps: int) -> dict:
    return {"id": rid, "activity_id": "row", "performed_at": "2026-03-02T10:00:00.000Z",
            "metrics": {"reps": {"value": reps}}}


def run(world: World, program: list) -> None:
    for name in REPLICAS:
        world.replica(name)
    world.author("laptop", {"intent": "define_activity", "record": {"id": "row", "name": "Row"}})
    for name in REPLICAS[1:]:
        world.sync("laptop", name)
    for op, r, *args in program:
        name = REPLICAS[r]
        world.tick(60)
        if op == "log":
            world.author(name, {"intent": "log_effort", "record": effort(args[0], args[1])})
        elif op == "edit":
            world.author(name, {"intent": "edit", "record": ["effort", args[0]],
                                "fields": {"metrics": {"reps": {"value": args[1]}}}})
        elif op == "remove":
            world.author(name, {"intent": "remove", "record": ["effort", args[0]]})
        elif op == "undo":
            held = [c for c in world.ordered(world.replicas[name].held) if c.events and c.writes
                    and all(w.kind == "effort" for w in c.writes)]
            if held:
                world.author(name, {"intent": "undo", "change": held[args[0] % len(held)].id})
        elif op == "sync":
            world.sync(name, REPLICAS[args[0]])
        elif op == "skew":
            world.clock(name).skew = timedelta(hours=args[0])


def settle(world: World, seed: int) -> list:
    """Deliver everything everywhere, each replica in its own random order and batches."""
    rng = random.Random(seed)
    every = world.ordered(set(world.changes))
    outcomes = []
    for name in REPLICAS:
        todo = [c for c in every if c.id not in world.replicas[name].held]
        rng.shuffle(todo)
        while todo:
            n = rng.randint(1, len(todo))
            outcomes += world.deliver(name, todo[:n])
            todo = todo[n:]
    return outcomes


def history(driver: str, program: list, seed: int):
    directory = tempfile.TemporaryDirectory()
    world = World(DRIVERS[driver](), directory.name)
    run(world, program)
    outcomes = settle(world, seed)
    return world, outcomes, directory


def snapshots(world: World) -> dict:
    return {name: world.snapshot(name) for name in REPLICAS}


@pytest.mark.baseline(v0="I1: orders that reject an undo end in different states")
@given(program=programs, seed=st.integers(0, 2**16))
@example(program=UNDO_FIRST, seed=1)
def test_i1_replicas_converge_whatever_the_delivery_order(driver, program, seed):
    world, _, directory = history(driver, program, seed)
    with directory:
        snaps = snapshots(world)
        world.close()
    first = snaps[REPLICAS[0]]
    for name in REPLICAS[1:]:
        assert equal(first, snaps[name]), diff(first, snaps[name])


@given(program=programs, seed=st.integers(0, 2**16))
def test_i2_rebuild_equals_incremental_state(driver, program, seed):
    world, _, directory = history(driver, program, seed)
    with directory:
        for name in REPLICAS:
            before = world.snapshot(name)
            world.rebuild(name)
            assert equal(before, world.snapshot(name)), diff(before, world.snapshot(name))
        world.close()


@pytest.mark.baseline(v0="I3, I4, I17: one edit silently wins, by wall clock, with no conflict shown")
@given(program=programs, seed=st.integers(0, 2**16))
@example(program=APART, seed=1)
@example(program=FAST_CLOCK, seed=1)
def test_i3_i4_i17_every_replica_matches_the_reference_model(driver, program, seed):
    world, _, directory = history(driver, program, seed)
    with directory:
        for name in REPLICAS:
            got, want = world.snapshot(name), world.expected(name)
            assert equal(got, want), diff(got, want)
        world.close()


@pytest.mark.baseline(v0="I5: an undo that arrives before its target is rejected")
@given(program=programs, seed=st.integers(0, 2**16))
@example(program=UNDO_FIRST, seed=1)
def test_i5_delivery_is_total_and_idempotent(driver, program, seed):
    world, outcomes, directory = history(driver, program, seed)
    with directory:
        assert [o for o in outcomes if o[0] == "rejected"] == []
        before = snapshots(world)
        again = [o for name in REPLICAS for o in world.deliver(name, world.ordered(set(world.changes)))]
        assert {o for o, _ in again} <= {"duplicate"}
        assert snapshots(world) == before
        world.close()
