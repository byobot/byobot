"""Review: a keeper whose database comes back from an older backup (Time Machine, a copied file) restarts its
epoch numbers below the folder's. Phones stay on the newer epoch, whose key the keeper no longer holds, so every
segment they send is deleted unread and sent again, and nothing from the phone arrives through Drive again."""

import sqlite3

from byobot.cloud import keeper as keeper_module
from byobot.cloud.folder import MemoryFolder
from byobot.cloud.keeper import Keeper
from byobot.core.store import Store

from test_cloud_keeper import Clock, event, joined


def backup(store, path):
    target = sqlite3.connect(path)
    store.conn.backup(target)
    target.close()


def test_a_keeper_restored_from_an_older_backup_still_hears_the_phone(store, tmp_path):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    relay = joined(keeper, folder)
    backup(store, tmp_path / "backup.db")  # last night's backup: epoch 1
    for _ in range(2):  # two compactions (or erases) since then; each time the phone moves and epoch n-1 goes
        keeper.compact()
        relay.sync()
        keeper.sync()
    assert keeper.epoch == relay.epoch == 3 and keeper.key(1) is None and keeper.key(2) is None
    store.close()

    restored = Keeper(Store(tmp_path / "backup.db"), folder, clock)  # the Mac comes back from the backup
    restored.sync()
    clock.now += keeper_module.MISSING_S
    restored.sync()  # its epoch file is gone from the folder, so it seeds the folder again
    sent = event()
    relay.log([sent])
    for _ in range(4):
        relay.sync()
        restored.sync()
    assert restored.store.event(sent["id"]), "the phone's set never reaches the restored Mac through Drive"
    assert relay.events == {}
