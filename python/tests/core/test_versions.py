"""S12: an older install meets a newer one's data, relays it, then upgrades. S13 and S14 are out of scope."""

from dataclasses import replace

import pytest

from .scenarios import edit, effort
from .snapshot import diff

VARIANTS = ("unknown_kind", "newer_version", "extra_field")


@pytest.mark.parametrize("variant", VARIANTS)
def test_s12_an_older_install_keeps_relays_and_later_projects_newer_data(world, variant):
    for name in ("new", "old", "relay", "fresh"):
        world.replica(name)
    newer = world.newer(world.author("new", effort()).id, variant)
    outcomes = world.deliver("old", [newer])
    assert [o for o, _ in outcomes if o == "rejected"] == []
    world.sync("old", "relay")
    world.upgrade("fresh", 2)
    world.sync("relay", "fresh")
    for name in ("old", "relay"):
        world.upgrade(name, 2)
    for name in ("old", "relay", "fresh"):
        assert diff(world.snapshot(name), world.expected(name)) == []
    assert world.snapshot("old") == world.snapshot("relay") == world.snapshot("fresh")


def test_s12_an_older_install_sees_no_conflict_through_a_newer_edit(world):
    """A v1 edit on top of a v2 edit on top of a v1 log: the first is an ancestor of the last, two steps back."""
    world.replica("new")
    world.replica("old")
    first = world.author("new", effort())
    middle = world.author("new", edit(reps=6))
    last = world.author("new", edit(reps=7))
    newer = world.newer(middle.id, "newer_version")
    [e] = last.events
    [w] = last.writes
    [m] = middle.writes
    last = replace(last, events=({**e, "prev": [newer.events[0]["id"]]},) if isinstance(e, dict) else
                   (replace(e, prior=(e.prior - {m.id}) | {newer.writes[0].id}),),
                   writes=(replace(w, prior=(w.prior - {m.id}) | {newer.writes[0].id}),))
    world.deliver("old", [first, newer, last])
    assert world.snapshot("old")["conflicts"] == {}
    assert diff(world.snapshot("old"), world.expected("old")) == []
