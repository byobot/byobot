"""Drive deep review: the keeper's sync writes the phone's key file, then Drive fails before the keeper enrols the
phone (the upload's answer lost and the check read failing, or the network dropping). If the code expires, or a new
code is shown, before the next sync, the keeper ends the join without the phone and deletes its device file, but
leaves its key file. The phone takes the key and reports itself joined; every segment it writes is skipped as "from a
device that isn't in this logbook" and deleted, and it sends it again, until the next epoch takes it off."""

import pytest

from _drive_helpers import PHONES, Clock, Flaky, effort
from byobot.cloud import files
from cloud_doubles import MemoryFolder
from byobot.cloud.keeper import JOIN_TTL_S, Keeper
from cloud_doubles import Relay


def _cut_short(store):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    phone = Relay.join(folder, keeper.join_code()["link"], PHONES[0])
    key_path = files.key_path(keeper.epoch, PHONES[0])
    create, read = folder.create, folder.read

    def lost(path, data):
        create(path, data)
        if path == key_path:
            raise Flaky("the answer to the key file's upload was lost")

    def down(path):
        if path == key_path:
            raise Flaky("can't reach Google Drive")
        return read(path)

    folder.create, folder.read = lost, down
    with pytest.raises(Flaky):
        keeper.sync()  # `cloud join` keeps syncing until the phone is in; the network drops
    folder.create, folder.read = create, read
    return keeper, phone, clock


def test_a_phone_that_took_its_key_gets_its_events_to_the_mac(store):
    keeper, phone, clock = _cut_short(store)
    clock.now += JOIN_TTL_S + 1  # back online after the code ran out
    keeper.sync()
    event = effort(PHONES[0], 1)
    phone.log([event])
    for _ in range(3):
        phone.sync()
        keeper.sync()
    assert phone.key is None or any(e["id"] == event["id"] for e in store.events()), \
        f"the phone counts itself in; the keeper says {keeper.last_join()} and skips what it writes"


def test_a_new_code_first_lets_in_the_phone_that_took_its_key(store):
    keeper, phone, _ = _cut_short(store)
    keeper.join_code()
    phone.sync()
    assert phone.key is not None and keeper.device(PHONES[0])
