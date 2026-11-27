"""Review: a phone taken off with `cloud remove` is told by the app to show the join code again to rejoin, and it
rejoins with the same device id and keys (apps/ios/Byobot/AppModel.swift, CloudSetup.forget). The keeper skips any
device id it ever removed, so that phone is never admitted again: the code just expires."""

from byobot.cloud.folder import MemoryFolder
from byobot.cloud.keeper import Keeper
from byobot.cloud.relay import Relay

from test_cloud_keeper import PHONE, Clock, event, joined


def test_a_removed_phone_can_join_again_with_a_new_code(store):
    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock())
    relay = joined(keeper, folder)
    keeper.remove(PHONE)
    relay.sync()
    assert relay.removed  # the app leaves Drive sync and says to show the join code again
    again = Relay.join(folder, keeper.join_code()["link"], PHONE, identity=relay.identity)  # same id, same keys
    report = keeper.sync()
    assert report.get("join") == "admitted", "the phone's answer to a fresh code is ignored, so it can't rejoin"
    again.sync()
    sent = event()
    again.log([sent])
    again.sync()
    keeper.sync()
    assert store.event(sent["id"])
