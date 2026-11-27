"""Drive deep review: background Drive sync (`serve`, `remote` and `mcp`) waits until `tried_at` is `every` seconds in
the past. A sync made while the Mac's clock ran ahead stores a `tried_at` in the future, so once the clock is set
right, background sync stops without a word for as long as the clock had been ahead."""

import time

from _drive_helpers import Clock
from cloud_doubles import MemoryFolder
from byobot.cloud.keeper import Keeper, due, tried
from byobot.commands import autosync


def test_background_sync_runs_again_once_a_clock_that_ran_ahead_is_set_right(store):
    folder = MemoryFolder()
    Keeper.setup(store, folder, Clock(time.time()))
    tried(store, clock=lambda: time.time() + 2 * 86400)  # a sync while the clock ran two days ahead
    assert due(store, 60), "no background sync for the next two days"
    assert autosync(store, folder) is not None
