"""Deep review (soak): the reference model must refuse what the store refuses, or the harness compares a replica
with an oracle that holds a write the store never made. tests/core only compares at quiescence, where an erase
hides the difference, so the mismatch shows only in a partly delivered state."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _soak_helpers import V1, ModelReplica, Refused, World, diff, effort, equal  # noqa: E402

from core.driver import Unsupported  # noqa: E402

H = "human:me"
ROW = {"intent": "define_activity", "record": {"id": "x.row", "name": "Row"}}
ERASE = {"intent": "erase", "record": ["effort", "e1"]}


def test_soak_the_model_refuses_logging_an_erased_id_again():
    m = ModelReplica("laptop")
    m.author(effort(), "2026-03-02T10:00:00.000Z", H)
    m.author(ERASE, "2026-03-02T10:01:00.000Z", H)
    with pytest.raises(Refused):
        m.author(effort(reps=1), "2026-03-02T10:02:00.000Z", H)


def test_soak_a_replica_holding_the_relog_but_not_the_erase_matches_the_model(tmp_path):
    world = World(V1(), tmp_path)
    try:
        world.replica("laptop")
        world.replica("phone")
        world.author("laptop", ROW)
        world.author("laptop", effort())
        world.sync("laptop", "phone")
        world.author("laptop", ERASE)
        relog = world.author("laptop", effort(reps=1))
        world.deliver("phone", [relog])  # the erase is still on its way
        got, want = world.snapshot("phone"), world.expected("phone")
        assert equal(got, want), diff(got, want)
        assert bool(relog.events) == bool(relog.writes), "the store and the model disagree on refusing the relog"
    finally:
        world.close()


def test_soak_undoing_a_change_the_store_refused_is_not_reported_as_no_equivalent(tmp_path):
    """tests/core turns Unsupported into a skip ("no equivalent"), so a refusal the model didn't make, followed by an
    undo of that change (as the persona's undo steps do), would skip S11, S21 or the synth checks instead of failing."""
    world = World(V1(), tmp_path)
    try:
        world.replica("laptop")
        world.author("laptop", ROW)
        world.author("laptop", effort())
        world.author("laptop", ERASE)
        relog = world.author("laptop", effort(reps=1))
        try:
            undo = world.author("laptop", {"intent": "undo", "change": relog.id})
        except Unsupported as exc:
            pytest.fail(f"reported as no equivalent: {exc}")
        assert undo.refused
    finally:
        world.close()
