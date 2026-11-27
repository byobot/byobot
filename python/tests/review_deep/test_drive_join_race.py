"""Drive deep review: `cloud join` writes its new code without the keeper lock, while `serve` may be in a sync that
already read the old, expired code. That sync then ends the join as expired and unsets the code `cloud join` just
wrote, so `cloud join` reports "expired" at once and the phone that scans the code shown is never let in."""

import json

from _drive_helpers import PHONES, Clock
from cloud_doubles import MemoryFolder, Relay
from byobot.cloud.keeper import JOIN_TTL_S, Keeper
from byobot.core.store import Store


def test_a_code_shown_while_serve_ends_the_last_one_still_lets_the_phone_in(store):
    folder = MemoryFolder()
    clock = Clock()
    serve = Keeper.setup(store, folder, clock)
    serve.join_code()  # an earlier `cloud join` that nobody answered
    clock.now += JOIN_TTL_S + 1
    join = Keeper(Store(store.path), folder, Clock(clock.now))  # `byobot cloud join` in another process
    shown = {}

    def serve_clock():
        if not shown:  # `cloud join` writes its code just after serve's sync read the old one
            shown.update(join.join_code())
        return clock.now

    serve.clock = serve_clock
    serve.sync()
    serve.clock = clock
    assert json.loads(join._get("join") or "null"), "the code `cloud join` just showed was unset"
    phone = Relay.join(folder, shown["link"], PHONES[0])
    join.sync()
    assert join.last_join() == {"join": "admitted", "device": PHONES[0]}
    phone.sync()
    assert phone.key is not None
