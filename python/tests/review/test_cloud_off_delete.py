"""Review: `cloud off --delete yes` and what it leaves in Drive and in the store."""

import time

from byobot.cloud.drive import Drive, DriveFolder
from byobot.cloud.keeper import Keeper
from byobot.cloud.relay import Relay

from fake_drive import FakeDrive
from test_cloud_keeper import PHONE, Clock, event


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

