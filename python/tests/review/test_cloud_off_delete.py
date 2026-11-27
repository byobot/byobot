"""Review: `cloud off --delete yes` and what it leaves in Drive and in the store."""

import time

from byobot.cloud import drive
from byobot.cloud.drive import Drive, DriveFolder
from byobot.cloud.folder import MemoryFolder
from byobot.cloud.keeper import Keeper
from byobot.cloud.relay import Relay
from byobot.core import commands
from byobot.core.store import Store
from byobot.util import ByobotError

from fake_drive import FakeDrive
from test_cloud_keeper import ME, PHONE, Clock, event


def folder_on(fake, tmp_path, store_id, who):
    stored = {"client_id": "cid", "client_secret": "secret", "refresh_token": "refresh-1", "access_token": "access-1",
              "expires_at": time.time() + 3600}
    return DriveFolder(store_id, Drive(fake, sleep=lambda s: None, stored=stored), journal=tmp_path / f"{who}.json")


def test_a_phone_segment_drive_still_hides_stays_in_drive_after_delete(store, tmp_path):
    """A phone uploads seconds before `cloud off --delete yes`; Drive's listing doesn't show it yet, so it is never
    deleted. The keeper is off from then on and the phone stops once it sees the closed epoch, so nothing ever deletes
    that file: the person's sets stay in Drive."""
    fake = FakeDrive()
    keeper = Keeper.setup(store, folder_on(fake, tmp_path, store.store_id, "mac"), Clock())
    relay = Relay.join(folder_on(fake, tmp_path, store.store_id, "phone"), keeper.join_code()["link"], PHONE)
    keeper.sync()
    relay.sync()
    before = set(fake.files)
    relay.log([event()])
    relay.sync()  # uploads a segment
    fake.lagging = set(fake.files) - before  # Drive's listings don't show it yet
    off = Keeper(store, folder_on(fake, tmp_path, store.store_id, "off"), Clock())  # `cloud off --delete yes`
    off.forget()
    fake.lagging = set()
    relay.sync()
    assert relay.removed
    left = [n for n in fake.names() if "/epochs/" not in n]
    assert left == [], f"still in Drive after cloud off --delete yes: {left}"


def test_a_restored_store_can_turn_drive_sync_on_again_after_off_delete(store, tmp_path):
    """After `cloud off --delete yes`, the folder keeps only the closed epoch file. A new computer restored from an
    export of this store (same store id) can then neither set up (the folder 'already has a keeper') nor recover
    (no recovery key is left), so Drive sync can never be turned on for this store again from there."""
    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock(), recovery=True)
    code = keeper.recovery_code
    commands.log(store, ME, [{"kind": "location", "id": "example-gym", "payload": {"name": "Example gym"}}])
    keeper.sync()
    keeper.forget()
    fresh = Store(tmp_path / "new-mac.db")
    fresh.restore(store.export())
    errors = []
    for attempt in (lambda: Keeper.setup(fresh, folder, Clock(), recovery=True),
                    lambda: Keeper.recover(fresh, folder, code, Clock())):
        try:
            attempt()
            return
        except ByobotError as exc:
            errors.append(exc.message)
    raise AssertionError(f"neither cloud setup nor cloud recover works: {errors}")


def test_a_closed_folder_is_no_logbook_to_recover(store, tmp_path):
    fake = FakeDrive()
    folder = folder_on(fake, tmp_path, store.store_id, "mac")
    keeper = Keeper.setup(store, folder, Clock())
    assert drive.stores(folder.drive) == {store.store_id}
    keeper.forget()
    assert drive.stores(folder.drive) == set()  # a new empty store here isn't sent to `cloud recover`
