"""Receipts: a relay drops what it sent only once the keeper lists the segment as read."""

import pytest

from cloud_doubles import MemoryFolder
from byobot.cloud.keeper import Keeper

from test_cloud_keeper import PHONE, Clock, event, joined

SAMPLES = {"samples": [{"source": "apple_health", "source_id": "S1", "start": "2026-11-23T07:00:00Z",
                        "type": "HKQuantityTypeIdentifierBodyMass", "value": 80.1, "unit": "kg"}]}


@pytest.fixture
def folder():
    return MemoryFolder()


@pytest.fixture
def keeper(store, folder):
    return Keeper.setup(store, folder, Clock())


def segments(folder):
    return sorted(p for p in folder.list() if p.startswith(f"segments/{PHONE}/"))


def test_the_plan_lists_the_segments_the_keeper_read(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.log([event()])
    relay.sync()
    keeper.sync()
    assert keeper.receipts() == {PHONE: {"read": [relay.seq]}}
    relay.sync()
    assert relay.receipts == {PHONE: {"read": [relay.seq]}} and relay.sent == {}


def test_a_segment_deleted_after_the_relay_looked_keeps_its_events(store, folder, keeper):
    relay = joined(keeper, folder)
    first, second = event(n=1), event(n=2)
    relay.log([first])
    relay.sync()
    gone = segments(folder)[0]
    relay.log([second])
    relay.sync()
    folder.delete(gone)
    keeper.sync()  # reads only the later segment
    view = {**folder.list(), gone: "as the relay last saw it"}
    relay._read_plan(view)
    relay._settle(view)
    assert first["id"] in relay.events and second["id"] not in relay.events
    relay.sync()
    keeper.sync()
    assert store.event(first["id"]) and store.event(second["id"])


def test_samples_in_a_segment_that_left_unread_are_sent_again(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.send_samples(SAMPLES)
    relay.sync()
    for p in segments(folder):
        folder.delete(p)
    keeper.sync()
    assert relay.sync()["written"] == 1
    keeper.sync()
    assert [r["kind"] for r in store.records("source_record")] == ["source_record"]
    relay.sync()
    assert relay.samples == [] and relay.sent == {}


def test_samples_not_yet_read_are_sent_again_in_a_new_epoch(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.send_samples(SAMPLES)
    relay.sync()
    for p in segments(folder):
        folder.delete(p)
    keeper.compact()
    relay.sync()  # moves to epoch 2 with the samples queued again
    keeper.sync()
    assert relay.epoch == 2 and [r["kind"] for r in store.records("source_record")] == ["source_record"]


def test_a_rejected_sample_batch_comes_back_with_why(store, folder, keeper):
    relay = joined(keeper, folder)
    bad = {"samples": [{**SAMPLES["samples"][0], "source": "whoop"}]}
    relay.log([event()])
    relay.send_samples(bad)
    relay.send_samples(SAMPLES)
    relay.sync()
    keeper.sync()
    seq = relay.seq
    assert keeper.receipts()[PHONE]["rejected"] == [{"seq": seq, "line": 1,
                                                    "reason": "a phone sends only apple_health samples"}]
    relay.sync()
    assert relay.rejected == [{"reason": "a phone sends only apple_health samples", "samples": {**bad, "deleted": []}}]
    assert relay.sent == {} and relay.samples == [] and relay.events == {}
    assert [r["kind"] for r in store.records("source_record")] == ["source_record"]
