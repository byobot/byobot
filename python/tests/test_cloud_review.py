"""What the first review of the cloud logbook found: nothing a relay sent is lost when a file vanishes or breaks."""

import pytest

from byobot.cloud import files
from byobot.cloud.keeper import Keeper
from byobot.core import commands
from byobot.core.relay import MAX_BATCH
from byobot.core.store import Store

from cloud_doubles import MemoryFolder
from test_cloud_keeper import ME, Clock, event, joined


@pytest.fixture
def folder():
    return MemoryFolder()


@pytest.fixture
def keeper(store, folder):
    return Keeper.setup(store, folder, Clock(), recovery=True)


def test_a_segment_deleted_before_it_was_read_is_sent_again(store, folder, keeper):
    relay = joined(keeper, folder)
    first, second = event(n=1), event(n=2)
    relay.log([first])
    relay.sync()
    relay.log([second])
    relay.sync()
    gone = sorted(p for p in folder.list() if p.startswith("segments/"))[0]
    folder.delete(gone)
    keeper.sync()  # holds the later event only
    relay.sync()
    assert first["id"] in relay.events
    keeper.sync()
    assert store.event(first["id"])
    relay.sync()
    assert relay.events == {}


def test_a_segment_the_keeper_cant_read_is_deleted_and_sent_again(store, folder, keeper):
    relay = joined(keeper, folder)
    first, second = event(n=1), event(n=2)
    relay.log([first])
    relay.sync()
    bad = next(p for p in folder.list() if p.startswith("segments/"))
    folder.files[bad] = folder.files[bad][:-3]
    relay.log([second])
    relay.sync()
    assert keeper.sync()["skipped"][0]["path"] == bad and bad not in folder.list()
    relay.sync()
    keeper.sync()
    assert store.event(first["id"]) and store.event(second["id"])


def test_a_relay_never_steps_back_to_an_older_plan(store, folder, keeper):
    relay = joined(keeper, folder)
    keeper.sync()
    older = relay.plan_path
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    keeper.sync()
    kept = {p: folder.files[p] for p in folder.list() if p.startswith("plan/")}
    relay.sync()
    newer = relay.plan_path
    assert newer > older
    for p in kept:
        folder.delete(p)
    folder.files[older] = b"replayed"  # an older plan put back can't be read as current
    relay.sync()
    assert relay.plan_path == newer


def test_sample_batches_over_the_limit_are_split(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.send_samples({"samples": [{"source": "apple_health", "source_id": f"S{i}", "start": "2026-11-23T07:00:00Z",
                                     "type": "HKQuantityTypeIdentifierStepCount", "value": i, "unit": "count"}
                                    for i in range(MAX_BATCH + 5)]})
    assert [len(b["samples"]) for b in relay.samples] == [MAX_BATCH, 5]
    relay.sync()
    keeper.sync()
    assert relay.samples == [] and len(store.records("source_record")) == MAX_BATCH + 5


def test_a_damaged_recovery_key_doesnt_stop_recovery(store, folder, keeper, tmp_path):
    keeper.compact()
    folder.files[files.recovery_path(1)] = folder.files[files.recovery_path(1)][:-5]
    again = Keeper.recover(Store(tmp_path / "new-mac.db"), folder, keeper.recovery_code, Clock())
    assert again.epoch == 3
