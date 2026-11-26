"""The keeper and a relay over a local folder: joining, sending both ways, the plan, and what gets refused."""

import json
import sqlite3

import pytest

from byobot.cloud import files, segment
from byobot.cloud.crypto import CloudError
from byobot.cloud.folder import Exists, LocalFolder, MemoryFolder
from byobot.cloud.keeper import Keeper
from byobot.cloud.relay import Relay, parse_join
from byobot.core import commands, hlc
from byobot.core.store import Store
from byobot.util import ByobotError, uuid7

PHONE = "0193a0b0-0000-7000-8000-00000000ab01"
OTHER = "0193a0b0-0000-7000-8000-00000000ab02"
ME = {"kind": "human", "id": "me"}


def event(device_id=PHONE, actor=ME, n=0, kind="effort"):
    return {"id": uuid7(), "schema_version": 1, "record": {"kind": kind, "id": uuid7()}, "op": "put",
            "kind_version": 1, "hlc": hlc.format((1795000000000 + 1000 * n, 0)), "ts": "2026-11-23T08:00:00-05:00",
            "device_id": device_id, "actor": actor, "prev": [],
            "payload": {"activity_id": "barbell_back_squat", "modality": "self_reported",
                        "time": {"start": "2026-11-23T07:40:00-05:00"}}}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def folder():
    return MemoryFolder()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def keeper(store, folder, clock):
    return Keeper.setup(store, folder, clock)


def joined(keeper, folder, device_id=PHONE):
    relay = Relay.join(folder, keeper.join_code()["link"], device_id)
    assert keeper.sync()["join"] == "admitted"
    relay.sync()
    assert relay.epoch == keeper.epoch and relay.key is not None
    return relay


def test_setup_starts_an_epoch_and_only_once(store, folder, keeper):
    listing = folder.list()
    assert files.epoch_path(1) in listing and any(p.startswith("plan/0001-") for p in listing)
    with pytest.raises(ByobotError, match="already"):
        Keeper.setup(store, folder)
    with pytest.raises(ByobotError, match="already has a keeper"):
        from byobot.core.store import Store
        Keeper.setup(Store(store.path.parent / "other.db"), folder)


def test_a_join_code_holds_no_key(keeper):
    code = keeper.join_code()
    assert parse_join(code["link"]) == {"store_id": code["store_id"], "secret": code["secret"],
                                        "keeper": code["keeper"]}
    assert keeper.key(1).hex() not in json.dumps(code)


def test_a_relay_joins_sends_and_learns_what_the_keeper_holds(store, folder, keeper):
    relay = joined(keeper, folder)
    sets = [event(n=i) for i in range(3)]
    relay.log(sets)
    assert relay.sync()["written"] == 1
    assert relay.sync()["written"] == 0  # already in a segment still in the folder
    keeper.sync()
    assert all(store.event(e["id"]) for e in sets)
    assert keeper.plan()["received"] == {PHONE: sets[-1]["hlc"]}
    relay.sync()
    assert relay.events == {} and relay.plan["received"][PHONE] == sets[-1]["hlc"]


def test_the_keeper_refuses_what_a_person_doesn_t_write_and_says_so(store, folder, keeper):
    relay = joined(keeper, folder)
    good, agent, forged = event(), event(actor={"kind": "agent", "id": "coach"}), event(device_id=OTHER)
    relay.log([good, agent, forged])
    relay.sync()
    keeper.sync()
    assert store.event(good["id"]) and not store.event(agent["id"]) and not store.event(forged["id"])
    relay.sync()
    assert {r["id"] for r in relay.plan["rejected"]} == {agent["id"], forged["id"]}


def test_a_relay_s_samples_become_source_records(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.send_samples({"samples": [{"source": "apple_health", "source_id": "S1", "start": "2026-11-23T07:00:00Z",
                                     "type": "HKQuantityTypeIdentifierBodyMass", "value": 80.1, "unit": "kg"}]})
    relay.sync()
    keeper.sync()
    assert [r["kind"] for r in store.records("source_record")] == ["source_record"]


def test_the_keeper_publishes_what_it_holds_once(store, folder, keeper):
    commands.log(store, ME, [{"kind": "location", "id": "example-gym", "payload": {"name": "Example gym"}}])
    assert keeper.sync()["published"] == 1
    assert keeper.sync()["published"] == 0
    keeper.publish_plan()
    assert len([p for p in folder.list() if p.startswith("plan/")]) == 1


def test_files_arriving_twice_or_out_of_order_change_nothing(store, folder, keeper):
    relay = joined(keeper, folder)
    first, second = event(n=1), event(n=2)
    relay.log([second])
    relay.sync()
    relay.log([first])
    relay.sync()
    keeper.sync()
    position = store.position()
    for path in [p for p in folder.list() if p.startswith(f"segments/{PHONE}/")]:
        store.conn.execute("DELETE FROM cloud_read WHERE path = ?", (path,))
    assert keeper.sync()["read"] == 2 and store.position() == position


def test_a_broken_segment_is_skipped_and_reported(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.log([event()])
    relay.sync()
    path = next(p for p in folder.list() if p.startswith("segments/"))
    folder.files[path] = folder.files[path][:-10]
    report = keeper.sync()
    assert [s["path"] for s in report["skipped"]] == [path] and "cut short" in report["skipped"][0]["reason"]


def test_a_segment_from_a_device_that_never_joined_is_ignored(store, folder, keeper):
    stranger = Relay(folder, store.store_id, OTHER, keeper.identity.public()["sign"], epoch=1, key=keeper.key(1))
    stranger.log([event(device_id=OTHER)])
    stranger.sync()
    report = keeper.sync()
    assert report["skipped"][0]["reason"] == "from a device that isn't in this logbook"


def test_a_relay_sends_again_what_vanished_before_the_keeper_held_it(store, folder, keeper):
    relay = joined(keeper, folder)
    sent = event()
    relay.log([sent])
    relay.sync()
    for p in [p for p in folder.list() if p.startswith("segments/")]:
        folder.delete(p)
    assert relay.sync()["written"] == 1
    keeper.sync()
    assert store.event(sent["id"])


def test_two_devices_answering_one_code_are_both_refused(store, folder, keeper):
    link = keeper.join_code()["link"]
    a, b = Relay.join(folder, link, PHONE), Relay.join(folder, link, OTHER)
    assert keeper.sync()["join"].startswith("refused")
    a.sync(), b.sync()
    assert a.key is None and b.key is None and keeper.devices()[1:] == []


def test_a_join_code_expires_and_a_wrong_proof_is_ignored(store, folder, keeper, clock):
    code = keeper.join_code()
    forged = code["link"].replace(code["secret"].replace("+", "%2B").replace("/", "%2F").replace("=", "%3D"),
                                  "AAAAAAAAAAAAAAAAAAAAAA%3D%3D")
    Relay.join(folder, forged, OTHER)
    assert "join" not in keeper.sync()
    clock.now += 601
    Relay.join(folder, code["link"], PHONE)
    assert keeper.sync()["join"] == "expired"
    assert [d["id"] for d in keeper.devices()] == [keeper.device_id]


def test_a_keeper_reopened_reads_nothing_twice(store, folder, keeper, clock):
    relay = joined(keeper, folder)
    relay.log([event()])
    relay.sync()
    assert keeper.sync()["read"] == 1
    again = Keeper(store, folder, clock)
    assert again.sync()["read"] == 0 and again.epoch == 1


def test_a_relay_trusts_only_the_keeper_s_signature(store, folder, keeper):
    from byobot.cloud.crypto import Identity
    impostor = Identity.generate()
    link = keeper.join_code()["link"].replace(keeper.identity.public()["sign"].replace("+", "%2B").replace(
        "/", "%2F").replace("=", "%3D"), impostor.public()["sign"].replace("+", "%2B").replace("/", "%2F")
        .replace("=", "%3D"))
    relay = Relay.join(folder, link, PHONE)
    keeper.sync()
    relay.sync()
    assert relay.key is None


def test_a_local_folder_never_replaces_a_file(tmp_path):
    folder = LocalFolder(tmp_path, "0193a0b0-0000-7000-8000-00000000c001")
    folder.create("plan/0001-0000000001.seg", b"one")
    with pytest.raises(Exists):
        folder.create("plan/0001-0000000001.seg", b"two")
    assert folder.read("plan/0001-0000000001.seg") == b"one"
    assert list(folder.list()) == ["plan/0001-0000000001.seg"]
    for bad in ("../x", "a//b", "/abs", "a/../b"):
        with pytest.raises(ValueError):
            folder.create(bad, b"")
    folder.delete("plan/0001-0000000001.seg")
    folder.delete("plan/0001-0000000001.seg")
    assert folder.list() == {}


def test_the_keeper_and_a_relay_work_over_a_local_folder(store, tmp_path, clock):
    folder = LocalFolder(tmp_path / "cloud", store.store_id)
    keeper = Keeper.setup(store, folder, clock)
    relay = joined(keeper, folder)
    sent = event()
    relay.log([sent])
    relay.sync()
    keeper.sync()
    assert store.event(sent["id"])
    with pytest.raises(CloudError):
        segment.read(b"junk", path="plan/0001-0000000001.seg", store_id=store.store_id, key=keeper.key(1),
                     sign_public=keeper.identity.public()["sign"])


class Busy(MemoryFolder):
    """Each upload is where the hub would write a set the phone pushed."""

    def __init__(self, db):
        super().__init__()
        self.db, self.blocked = db, []

    def create(self, path, data):
        other = sqlite3.connect(self.db, isolation_level=None, timeout=0.1)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        except sqlite3.OperationalError:
            self.blocked.append(path)
        finally:
            other.close()
        super().create(path, data)


def test_uploads_leave_the_store_free_for_other_writers(store):
    folder = Busy(store.path)
    keeper = Keeper.setup(store, folder, Clock())
    commands.log(store, ME, [{"kind": "location", "id": "example-gym", "payload": {"name": "x"}}])
    folder.blocked.clear()
    keeper.sync()
    keeper.compact()
    assert folder.blocked == []


class Flaky(MemoryFolder):
    """Key uploads fail outright, or land with their answer lost."""

    def __init__(self):
        super().__init__()
        self.fail = self.lose = False

    def create(self, path, data):
        if self.fail and path.startswith("keys/"):
            raise ByobotError("can't reach Google Drive")
        super().create(path, data)
        if self.lose and path.startswith("keys/"):
            self.lose = False
            raise ByobotError("can't reach Google Drive")


def test_a_failed_key_upload_keeps_the_join_code(store):
    folder = Flaky()
    keeper = Keeper.setup(store, folder, Clock())
    relay = Relay.join(folder, keeper.join_code()["link"], PHONE)
    folder.fail = True
    with pytest.raises(ByobotError):
        keeper.sync()
    folder.fail = False
    assert keeper.sync()["join"] == "admitted"
    relay.sync()
    assert relay.key is not None


def test_a_key_upload_whose_answer_was_lost_still_admits_the_phone(store):
    folder = Flaky()
    keeper = Keeper.setup(store, folder, Clock())
    relay = Relay.join(folder, keeper.join_code()["link"], PHONE)
    folder.lose = True
    assert keeper.sync()["join"] == "admitted"
    sent = event()
    relay.log([sent])
    relay.sync()
    keeper.sync()
    assert store.event(sent["id"])


def test_another_process_admitting_the_phone_ends_the_join(store):
    folder = MemoryFolder()
    cli = Keeper.setup(store, folder, Clock())
    serve = Keeper(Store(store.path), folder, Clock())
    cli.join_code()
    assert cli.last_join() is None
    Relay.join(folder, cli.join_code()["link"], PHONE)
    serve.sync()
    assert cli.last_join() == {"join": "admitted", "device": PHONE}
