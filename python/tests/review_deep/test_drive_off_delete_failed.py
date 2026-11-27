"""Drive deep review: `cloud off --delete yes` that fails partway (Drive answers an error while it deletes files)
has already written the closed epoch file, so every phone stops as if it was taken off, while the keeper carries on
in the old epoch, waits on the phones forever, and fails every later epoch start."""

import pytest

from _drive_helpers import Clock, ME, joined
from byobot.cloud.drive import UpstreamError
from byobot.cloud.folder import MemoryFolder
from byobot.cloud.keeper import Keeper
from byobot.core import commands
from byobot.util import ByobotError


def _off_delete_fails_partway(store):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    relay = joined(keeper, folder)
    real, calls = folder.delete, []

    def flaky(path):
        calls.append(path)
        if len(calls) == 3:
            raise UpstreamError("can't reach Google Drive: connection reset")
        real(path)

    folder.delete = flaky
    with pytest.raises(ByobotError):
        keeper.forget()  # `cloud off --delete yes` answers an error
    folder.delete = real
    return keeper, relay


def test_a_failed_off_delete_leaves_keeper_and_phone_agreeing(store):
    keeper, relay = _off_delete_fails_partway(store)
    for _ in range(2):  # the person runs `cloud sync`, or `serve` syncs on its own
        keeper.sync()
        relay.sync()
    assert keeper.is_off or (not relay.removed and relay.epoch == keeper.epoch), \
        "the phone was told it was taken off Drive sync while the keeper still counts on it"


def test_after_a_failed_off_delete_the_next_epoch_still_starts(store):
    keeper, relay = _off_delete_fails_partway(store)
    keeper.sync()
    commands.log(store, ME, [{"kind": "location", "id": "example-gym", "payload": {"name": "Gym"}}])
    commands.erase(store, ME, "location", "example-gym")
    report = keeper.sync()  # an erase starts the next epoch, whose number the closed marker already took
    assert keeper.is_off or report.get("new_epoch"), report
