"""Drive deep review: an epoch start whose epoch file reached Drive but whose run then failed (the upload's answer
lost while the network was down, or the process killed) leaves `starting` equal to the current epoch. The next start
before a sync clears it (`cloud compact` run again, `cloud remove`) numbers its epoch as that same, live one, reuses
its key, and deletes its key files, checkpoint and plan as "leftovers" before writing them again.

* If that run fails partway, the next sync sees the epoch file and calls the epoch done: a phone that hadn't taken
  its key never can, cleanup waits on it for good, and the phone gets no plan or receipts again.
* `cloud remove` fails on the epoch file and starts no new epoch: the removed phone keeps the key the keeper goes on
  writing with."""

import pytest

from _drive_helpers import PHONES, Clock, Flaky, joined
from byobot.cloud import files
from byobot.cloud.folder import MemoryFolder
from byobot.cloud.keeper import Keeper
from byobot.util import ByobotError
from fake_drive import Killed


def test_a_phone_still_moves_after_a_failed_compact_is_run_again(store, monkeypatch):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    phone = joined(keeper, folder, PHONES[0])
    real = Keeper._write

    def cut_short(self, path, data):
        real(self, path, data)
        if path == files.epoch_path(2):
            raise Killed("the run ended right after epoch 2's file landed")

    monkeypatch.setattr(Keeper, "_write", cut_short)
    with pytest.raises(Killed):
        keeper.compact()  # `cloud compact`
    monkeypatch.setattr(Keeper, "_write", real)
    create, calls = folder.create, []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 1:
            raise Flaky("Google Drive answered 503")
        create(path, data)

    folder.create = flaky
    with pytest.raises(ByobotError):
        keeper.compact()  # run again, since it failed; Drive fails once more partway
    folder.create = create
    for _ in range(3):
        keeper.sync()
        phone.sync()
    assert phone.epoch == keeper.epoch, f"the phone is stuck in epoch {phone.epoch}, the keeper in {keeper.epoch}"
    assert keeper.status()["cleanup_waits_on"] == []


def test_a_removed_phone_cant_read_what_the_keeper_writes_next(store, monkeypatch):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    lost = joined(keeper, folder, PHONES[0])
    real = Keeper._write

    def cut_short(self, path, data):
        real(self, path, data)
        if path == files.epoch_path(2):
            raise Killed("the run ended right after epoch 2's file landed")

    monkeypatch.setattr(Keeper, "_write", cut_short)
    with pytest.raises(Killed):
        keeper.compact()
    monkeypatch.setattr(Keeper, "_write", real)
    lost.sync()
    assert lost.epoch == 2

    try:
        keeper.remove(lost.device_id)  # `cloud remove` for the lost phone
    except ByobotError:
        pass  # and running it again says there is no such device
    keeper.sync()
    lost.sync()
    assert keeper.key(keeper.epoch) != lost.key, "the removed phone holds the key of the epoch the keeper writes in"


def test_the_same_after_drive_loses_the_epoch_file_upload_s_answer(store, tmp_path):
    from byobot.cloud import drive as drive_module
    from byobot.cloud.drive import UpstreamError
    from byobot.cloud.relay import Relay
    from fake_drive import FakeDrive
    from test_cloud_drive import folder_on

    fake = FakeDrive()
    keeper = Keeper.setup(store, folder_on(fake, tmp_path, store.store_id), Clock())
    lost = Relay.join(folder_on(fake, tmp_path, store.store_id, who="phone"), keeper.join_code()["link"], PHONES[0])
    keeper.sync()
    lost.sync()
    fake.fail("POST /upload", "pass", times=2)  # epoch 2's key file and plan land
    fake.fail("POST /upload", "lost")  # the epoch file lands, its answer is lost, and Drive stays down
    fake.fail("POST /upload", 503, times=drive_module.TRIES - 1)
    with pytest.raises(UpstreamError):
        keeper.compact()
    assert any(f["name"].endswith("/epochs/0000000002.json") for f in fake.files.values())
    fake.faults.clear()
    lost.sync()
    try:
        keeper.remove(lost.device_id)
    except ByobotError:
        pass
    keeper.sync()
    lost.sync()
    assert keeper.key(keeper.epoch) != lost.key, "the removed phone holds the key of the epoch the keeper writes in"
