"""S1 to S5: sync and ordering. S5's network faults and S6's crashes come with the fault harness."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations

import pytest

from .harness import START, Change
from .scenarios import both_ways, edit, effort, reps
from .snapshot import diff

TO_2001 = datetime(2001, 1, 1, tzinfo=timezone.utc) - datetime.fromisoformat(START.replace("Z", "+00:00"))

CLOCKS = [
    pytest.param({}, id="both_correct"),
    pytest.param({"phone": timedelta(hours=3)}, id="phone_3h_fast"),
    pytest.param({"phone": timedelta(hours=-3)}, id="phone_3h_slow"),
    pytest.param({"phone": TO_2001}, id="phone_reset_to_2001"),
    pytest.param({"laptop": timedelta(days=-40)}, id="laptop_skewed"),
]


def apart(world, *names):
    for name in names:
        world.replica(name)
    world.author(names[0], effort())
    both_ways(world, *names)


def converged(world, *names):
    for name in names:
        assert diff(world.snapshot(name), world.expected(name)) == []
    assert all(world.expected(n) == world.expected(names[0]) for n in names)


# -- S1. offline edits of one record on two devices ------------------------------------------------


@pytest.mark.parametrize("skews", CLOCKS)
def test_s1_an_edit_made_after_seeing_another_wins_whatever_the_clocks(world, skews):
    apart(world, "phone", "laptop")
    for name, skew in skews.items():
        world.clock(name).skew = skew
    world.author("phone", edit(reps=5 + 4))
    world.sync("phone", "laptop")
    world.tick(3600)
    world.author("laptop", edit(reps=9 + 3))
    both_ways(world, "phone", "laptop")
    assert reps(world, "phone") == reps(world, "laptop") == 12
    converged(world, "phone", "laptop")


def test_s1_a_clock_jumping_back_mid_session_keeps_the_device_order(world):
    apart(world, "phone", "laptop")
    world.author("phone", edit(reps=7))
    world.clock("phone").skew = timedelta(hours=-2)
    world.author("phone", edit(reps=8))
    both_ways(world, "phone", "laptop")
    assert reps(world, "laptop") == 8
    converged(world, "phone", "laptop")


def test_s1_identical_timestamps_still_order_by_causality(world):
    apart(world, "phone", "laptop")
    frozen = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)
    world.clock("phone").frozen = world.clock("laptop").frozen = frozen
    world.author("phone", edit(reps=7))
    world.sync("phone", "laptop")
    world.author("laptop", edit(reps=8))
    both_ways(world, "phone", "laptop")
    assert reps(world, "phone") == 8
    converged(world, "phone", "laptop")


@pytest.mark.parametrize("first", ["phone", "laptop"])
def test_s1_concurrent_edits_are_a_visible_conflict(world, first):
    apart(world, "phone", "laptop")
    world.author("phone", edit(reps=7))
    world.author("laptop", edit(reps=8))
    other = "laptop" if first == "phone" else "phone"
    world.sync(first, other)
    world.sync(other, first)
    assert list(world.snapshot("phone")["conflicts"]) == ["effort/e1"]
    converged(world, "phone", "laptop")


def test_s1_identical_concurrent_edits_do_not_conflict(world):
    apart(world, "phone", "laptop")
    world.author("phone", edit(reps=7))
    world.author("laptop", edit(reps=7))
    both_ways(world, "phone", "laptop")
    assert world.snapshot("phone")["conflicts"] == {}
    converged(world, "phone", "laptop")


def test_s1_sequential_edits_on_one_device_never_conflict(world):
    apart(world, "phone", "laptop")
    for n in (6, 7, 8):
        world.author("phone", edit(reps=n))
    both_ways(world, "phone", "laptop")
    assert world.snapshot("laptop")["conflicts"] == {} and reps(world, "laptop") == 8
    converged(world, "phone", "laptop")


def test_s1_edits_on_three_devices_are_one_conflict(world):
    apart(world, "phone", "laptop", "tablet")
    for n, name in enumerate(("phone", "laptop", "tablet")):
        world.author(name, edit(reps=7 + n))
    both_ways(world, "phone", "laptop", "tablet")
    assert len(world.snapshot("tablet")["conflicts"].get("effort/e1", [])) == 3
    converged(world, "phone", "laptop", "tablet")


def test_s1_resolving_while_a_third_edit_is_in_flight_conflicts_again(world):
    apart(world, "phone", "laptop", "tablet")
    world.author("phone", edit(reps=7))
    world.author("laptop", edit(reps=8))
    both_ways(world, "phone", "laptop")
    world.author("phone", {"intent": "resolve", "record": ["effort", "e1"], "choice": 0})
    world.author("tablet", edit(reps=9))
    both_ways(world, "phone", "laptop", "tablet")
    assert len(world.snapshot("laptop")["conflicts"]["effort/e1"]) == 2
    converged(world, "phone", "laptop", "tablet")


def test_s1_undoing_a_resolve_brings_the_conflict_back(world):
    apart(world, "phone", "laptop")
    world.author("phone", edit(reps=7))
    world.author("laptop", edit(reps=8))
    both_ways(world, "phone", "laptop")
    fixed = world.author("phone", {"intent": "resolve", "record": ["effort", "e1"], "choice": 0})
    world.author("phone", {"intent": "undo", "change": fixed.id})
    both_ways(world, "phone", "laptop")
    assert len(world.snapshot("laptop")["conflicts"]["effort/e1"]) == 2
    converged(world, "phone", "laptop")


def test_s1_two_devices_resolving_differently_conflict_and_converge(world):
    apart(world, "phone", "laptop")
    world.author("phone", edit(reps=7))
    world.author("laptop", edit(reps=8))
    both_ways(world, "phone", "laptop")
    world.author("phone", {"intent": "resolve", "record": ["effort", "e1"], "choice": 0})
    world.author("laptop", {"intent": "resolve", "record": ["effort", "e1"], "choice": 1})
    both_ways(world, "phone", "laptop")
    assert "effort/e1" in world.snapshot("phone")["conflicts"]
    converged(world, "phone", "laptop")


# -- S2. undo or remove racing an edit ---------------------------------------------------------------


def test_s2_undo_racing_an_edit_converges_in_every_delivery_order(world):
    a, b = "a", "b"
    world.replica(a)
    world.replica(b)
    log = world.author(a, effort())
    world.sync(a, b)
    mine = world.author(a, edit(reps=6))
    theirs = world.author(b, edit(reps=7))
    undo = world.author(a, {"intent": "undo", "change": mine.id})
    history = [log, mine, theirs, undo]
    snapshots = set()
    for i, order in enumerate(permutations(history)):
        name = f"r{i}"
        world.replica(name)
        outcomes = [o for c in order for o in world.deliver(name, [c])]
        assert [o for o, _ in outcomes if o == "rejected"] == []
        assert diff(world.snapshot(name), world.expected(name)) == []
        snapshots.add(repr(sorted(world.snapshot(name)["records"].items())))
    assert len(snapshots) == 1


def test_s2_remove_after_several_edits_leaves_nothing(world):
    world.replica("phone")
    world.replica("laptop")
    world.author("phone", effort())
    world.author("phone", edit(reps=6))
    world.author("laptop", effort("e2"))
    world.author("phone", edit(reps=7))
    world.author("phone", {"intent": "remove", "record": ["effort", "e1"]})
    both_ways(world, "phone", "laptop")
    assert reps(world, "laptop") is None and reps(world, "laptop", "e2") == 5
    converged(world, "phone", "laptop")


def test_s2_undoing_an_undo_brings_the_edit_back(world):
    world.replica("phone")
    world.replica("laptop")
    world.author("phone", effort())
    change = world.author("phone", edit(reps=6))
    undo = world.author("phone", {"intent": "undo", "change": change.id})
    assert reps(world, "phone") == 5
    world.author("phone", {"intent": "undo", "change": undo.id})
    both_ways(world, "phone", "laptop")
    assert reps(world, "laptop") == 6
    converged(world, "phone", "laptop")


# -- S3. two devices undo the same event --------------------------------------------------------------


def test_s3_two_devices_undoing_one_edit_agree_without_a_conflict(world):
    apart(world, "phone", "laptop")
    change = world.author("phone", edit(reps=6))
    world.sync("phone", "laptop")
    world.author("phone", {"intent": "undo", "change": change.id})
    world.author("laptop", {"intent": "undo", "change": change.id})
    outcomes = world.sync("phone", "laptop") + world.sync("laptop", "phone")
    assert [o for o, _ in outcomes if o == "rejected"] == []
    assert world.snapshot("phone")["conflicts"] == {} and reps(world, "phone") == 5
    converged(world, "phone", "laptop")


# -- S4. duplicate and overlapping delivery -------------------------------------------------------------


def test_s4_redelivery_is_a_reported_no_op(world):
    world.replica("phone")
    world.replica("laptop")
    sent = [world.author("phone", effort(f"e{n}")) for n in range(1, 4)]
    world.deliver("laptop", sent[:2])
    before = world.snapshot("laptop")
    outcomes = world.deliver("laptop", sent[1:]) + world.deliver("laptop", sent)
    assert [o for o, _ in outcomes] == ["duplicate", "accepted"] + ["duplicate"] * 3
    assert world.snapshot("laptop")["records"].keys() == before["records"].keys() | {"effort/e3"}
    converged(world, "laptop")


# -- S5. corrupted batches, at the core ------------------------------------------------------------------


def _tamper(event):
    if isinstance(event, dict):
        return {**event, "payload": {**event["payload"], "metrics": {"reps": {"value": 50}}}}
    return replace(event, content={**event.content, "metrics": {"reps": {"value": 50}}})


def test_s5_an_id_reused_with_other_content_is_rejected_and_its_neighbours_kept(world):
    world.replica("phone")
    world.replica("laptop")
    first = world.author("phone", effort())
    second = world.author("phone", effort("e2"))
    world.deliver("laptop", [first])
    forged = Change("forged", "phone", tuple(_tamper(e) for e in first.events),
                    tuple(_tamper(w) for w in first.writes))
    outcomes = world.deliver("laptop", [forged, second])
    assert [o for o, _ in outcomes] == ["rejected", "accepted"]
    assert reps(world, "laptop") == 5 and reps(world, "laptop", "e2") == 5
