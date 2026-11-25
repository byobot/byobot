"""S12: an older install meets a newer one's data, relays it, then upgrades. S13 and S14 are out of scope."""

import pytest

from .scenarios import effort
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
