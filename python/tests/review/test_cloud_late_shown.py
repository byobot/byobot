"""Review: an event the keeper read from a retired epoch's segment counts as shown in the folder, so once cleanup
deletes that segment the event is in no file of the folder, and recovery from the folder loses it."""

from byobot.cloud.keeper import Keeper
from cloud_doubles import MemoryFolder
from byobot.core.store import Store

from test_cloud_keeper import Clock, event, joined


def test_an_event_read_late_from_a_retired_epoch_stays_in_the_folder(store, tmp_path):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock, recovery=True)
    code = keeper.recovery_code
    relay = joined(keeper, folder)
    sent = event()
    relay.log([sent])
    relay.sync()  # a segment in epoch 1
    keeper.compact()  # `cloud compact` reads nothing first: the checkpoint of epoch 2 lacks the phone's set
    keeper.sync()  # reads the epoch-1 segment late; the plan's receipt lists it
    relay.sync()  # takes key 2, sees its segment read, forgets the set, writes in epoch 2
    assert relay.epoch == 2 and relay.events == {}
    keeper.sync()  # every device moved: epoch 1's files, the set's segment among them, are deleted
    assert store.event(sent["id"])
    assert not any(p.startswith("segments/") and "/0000000001-" in p for p in folder.list())

    fresh = Store(tmp_path / "new-mac.db")
    Keeper.recover(fresh, folder, code, clock)
    assert fresh.event(sent["id"]), "the phone's set is in no file of the folder, so recovery lost it"
