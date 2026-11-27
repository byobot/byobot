"""Review: a segment from a newer phone holding a message this byobot doesn't know yet is deleted, not kept until
the Mac is upgraded, so the phone and the Mac loop uploading and deleting it, and its events never land."""

from byobot.cloud import segment
from byobot.cloud.keeper import Keeper

from cloud_doubles import MemoryFolder
from test_cloud_keeper import PHONE, Clock, event, joined


def write_newer(relay, messages):
    relay.seq += 1
    path = segment.name("segment", relay.epoch, relay.seq, PHONE)
    relay.folder.create(path, segment.write("segment", relay.store_id, PHONE, relay.epoch, relay.seq, messages,
                                            relay.key, relay.identity))
    return path


def test_an_older_keeper_keeps_a_segment_with_a_newer_message_until_it_is_upgraded(store):
    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock())
    relay = joined(keeper, folder)
    sent = event()
    path = write_newer(relay, [{"events": {"events": [sent]}}, {"notes": {"text": "a message a later byobot adds"}}])
    report = keeper.sync()
    assert report["skipped"][0]["path"] == path and "upgrade" in report["skipped"][0]["reason"]
    assert path in folder.list(), "a newer phone's segment was deleted, so it is sent and deleted again forever"
    assert "upgrade" in keeper.local_status()
