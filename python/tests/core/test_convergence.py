"""I1 to I5 on generated histories: offline edits, undos and removes on several devices, any delivery order."""

import random
import tempfile
from contextlib import contextmanager
from datetime import timedelta

from hypothesis import example, given
from hypothesis import strategies as st

from .conftest import DRIVERS
from .harness import World
from .snapshot import diff, equal

REPLICAS = ("laptop", "phone", "tablet")
IDS = ("e1", "e2")

edit = st.tuples(st.just("edit"), st.integers(0, 2), st.sampled_from(IDS), st.integers(1, 9))
sync = st.tuples(st.just("sync"), st.integers(0, 2), st.integers(0, 2))
step = st.one_of(
    st.tuples(st.just("log"), st.integers(0, 2), st.sampled_from(IDS), st.integers(1, 9)),
    edit, edit, edit,
    st.tuples(st.just("undo"), st.integers(0, 2), st.integers(0, 50)),
    st.tuples(st.just("remove"), st.integers(0, 2), st.sampled_from(IDS)),
    sync, sync,
    st.tuples(st.just("skew"), st.integers(0, 2), st.integers(-48, 48)),
)
programs = st.lists(step, min_size=1, max_size=14)
# Resolve whatever the replica shows in conflict; undo its own last change, a resolve included; erase.
resolve = st.tuples(st.just("resolve"), st.integers(0, 2), st.integers(0, 9), st.integers(0, 3))
undo_last = st.tuples(st.just("undo_last"), st.integers(0, 2))
erase = st.tuples(st.just("erase"), st.integers(0, 2), st.sampled_from(IDS))
with_resolves = st.lists(st.one_of(step, resolve, resolve, undo_last, erase), min_size=1, max_size=20)

# An undo synced before the event it undoes.
UNDO_FIRST = [("log", 0, "e1", 5), ("sync", 0, 1), ("edit", 1, "e1", 9), ("undo", 1, 1), ("sync", 1, 2)]
# The phone, three hours fast, edits first; the laptop edits later having seen it.
FAST_CLOCK = [("log", 0, "e1", 5), ("sync", 0, 1), ("skew", 1, 3), ("edit", 1, "e1", 5), ("sync", 1, 0),
              ("edit", 0, "e1", 9)]
# Two devices edit one set while apart.
APART = [("log", 0, "e1", 5), ("sync", 0, 1), ("edit", 0, "e1", 6), ("edit", 1, "e1", 9)]
# Two devices log the same set alike, then one undoes its own: the other's still stands.
SAME_LOG_UNDONE = [("log", 0, "e1", 2), ("log", 2, "e1", 2), ("sync", 2, 0), ("undo", 0, 1)]
# A set logged again while its removal is in conflict keeps the conflict.
RELOG_OVER_CONFLICT = [("log", 0, "e1", 1), ("log", 2, "e1", 2), ("undo", 0, 0), ("sync", 2, 0), ("log", 0, "e1", 1)]


def effort(rid: str, reps: int) -> dict:
    return {"id": rid, "activity_id": "x.row", "performed_at": "2026-03-02T10:00:00.000Z",
            "metrics": {"reps": {"value": reps}}}


def run(world: World, program: list, seeded: bool = False) -> None:
    for name in REPLICAS:
        world.replica(name)
    world.author("laptop", {"intent": "define_activity", "record": {"id": "x.row", "name": "Row"}})
    if seeded:  # every set already logged and synced, so edits race from the start
        for rid in IDS:
            world.author("laptop", {"intent": "log_effort", "record": effort(rid, 5)})
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
        elif op == "resolve":
            shown = world.expected(name)["conflicts"]
            target = args[0] if args[0] in IDS else sorted(shown)[args[0] % len(shown)].split("/")[1] if shown else None
            versions = shown.get(f"effort/{target}", [])
            if versions:
                choice = "remove" if args[1] == len(versions) or args[1] > 2 else args[1] % len(versions)
                world.author(name, {"intent": "resolve", "record": ["effort", target], "choice": choice})
        elif op == "undo_last":
            own = [c for c in world.ordered(world.replicas[name].held) if c.replica == name and c.events
                   and c.writes and all(w.kind == "effort" for w in c.writes)]
            if own:
                world.author(name, {"intent": "undo", "change": own[-1].id})
        elif op == "erase":
            world.author(name, {"intent": "erase", "record": ["effort", args[0]]})
        elif op == "skew":
            world.clock(name).skew = timedelta(hours=args[0])


def settle(world: World, seed: int, check: bool = False) -> list:
    """Deliver everything everywhere, each replica in its own random order and batches; `check` compares each
    replica with the model after every batch."""
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
            if check:
                got, want = world.snapshot(name), world.expected(name)
                assert equal(got, want), diff(got, want)
    return outcomes


@contextmanager
def history(driver: str, program: list, seed: int, seeded: bool = False, check: bool = False):
    with tempfile.TemporaryDirectory() as directory:
        world = World(DRIVERS[driver](), directory)
        try:
            run(world, program, seeded)
            yield world, settle(world, seed, check)
        finally:
            world.close()


def snapshots(world: World) -> dict:
    return {name: world.snapshot(name) for name in REPLICAS}


@given(program=programs, seed=st.integers(0, 2**16))
@example(program=UNDO_FIRST, seed=1)
def test_i1_replicas_converge_whatever_the_delivery_order(driver, program, seed):
    with history(driver, program, seed) as (world, _):
        snaps = snapshots(world)
    first = snaps[REPLICAS[0]]
    for name in REPLICAS[1:]:
        assert equal(first, snaps[name]), diff(first, snaps[name])


@given(program=programs, seed=st.integers(0, 2**16))
def test_i2_rebuild_equals_incremental_state(driver, program, seed):
    with history(driver, program, seed) as (world, _):
        for name in REPLICAS:
            before = world.snapshot(name)
            world.rebuild(name)
            assert equal(before, world.snapshot(name)), diff(before, world.snapshot(name))


@given(program=programs, seed=st.integers(0, 2**16))
@example(program=APART, seed=1)
@example(program=FAST_CLOCK, seed=1)
@example(program=SAME_LOG_UNDONE, seed=0)
@example(program=RELOG_OVER_CONFLICT, seed=0)
def test_i3_i4_i17_every_replica_matches_the_reference_model(driver, program, seed):
    with history(driver, program, seed) as (world, _):
        for name in REPLICAS:
            got, want = world.snapshot(name), world.expected(name)
            assert equal(got, want), diff(got, want)


@given(program=programs, seed=st.integers(0, 2**16))
@example(program=UNDO_FIRST, seed=1)
def test_i5_delivery_is_total_and_idempotent(driver, program, seed):
    with history(driver, program, seed) as (world, outcomes):
        assert [o for o in outcomes if o[0] == "rejected"] == []
        before = snapshots(world)
        again = [o for name in REPLICAS for o in world.deliver(name, world.ordered(set(world.changes)))]
        assert {o for o, _ in again} <= {"duplicate"}
        assert snapshots(world) == before


@given(program=with_resolves, seed=st.integers(0, 2**16))
@example(program=APART + [("sync", 0, 1), ("sync", 1, 0), ("resolve", 0, "e1", 1), ("resolve", 1, "e1", 0)], seed=1)
def test_i17_resolutions_converge_and_match_the_reference_model(driver, program, seed):
    with history(driver, program, seed, seeded=True, check=True) as (world, outcomes):
        assert [o for o in outcomes if o[0] == "rejected"] == []
        for name in REPLICAS:
            got, want = world.snapshot(name), world.expected(name)
            assert equal(got, want), diff(got, want)
