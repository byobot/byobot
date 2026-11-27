"""Review: a phone that hasn't pulled since the laptop erased a slug writes a new record under it, and is told it was
accepted while its content is blanked (the CLI refuses the same write with a Conflict)."""

from byobot.core import commands, relay

from test_serve import ME, PHONE, _event


def test_a_new_location_under_an_erased_slug_is_not_reported_as_kept(store):
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    commands.erase(store, ME, "location", "example_gym")

    # On the phone, which last pulled before the erase, you add "Example gym" again: same slug, prev [].
    added = _event("location", "example_gym", {"name": "Example gym"})
    [result] = relay.accept_events(store, PHONE, [added])

    kept = store.get("location", "example_gym") is not None
    assert kept or result["status"] == "rejected", (
        f"the hub answered {result['status']!r} but kept nothing; the phone drops the location at its next pull")
