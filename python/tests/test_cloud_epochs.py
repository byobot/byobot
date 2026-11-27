"""New epochs: after an erase, a removal, too many files or a wiped folder; cleanup; and recovery from the code."""

import json

import pytest

from byobot.cloud import keeper as keeper_module
from byobot.cloud import files, segment
from byobot.cloud.crypto import CloudError, Identity, recovery_code
from byobot.cloud.keeper import Keeper
from byobot.core import commands
from byobot.core.store import Store
from byobot.util import ByobotError

from cloud_doubles import MemoryFolder, Relay
from test_cloud_keeper import ME, OTHER, PHONE, Clock, event, joined

SECRET = "zq-erase-me-7f3a"


@pytest.fixture
def folder():
    return MemoryFolder()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def keeper(store, folder, clock):
    return Keeper.setup(store, folder, clock, recovery=True)


def opened(folder, keeper, key):
    """Every segment in the folder that the key opens, as text."""
    out = []
    for path in folder.list():
        where = segment.parse_name(path)
        if where is None:
            continue
        writer = where.get("device_id") or keeper.device_id
        device = keeper.device(writer) or {"sign": keeper.identity.public()["sign"]}
        try:
            out.append(json.dumps(segment.read(folder.read(path), path=path, store_id=keeper.store.store_id,
                                               key=key, sign_public=device["sign"])[1]))
        except CloudError:
            pass
    return out


def raw(store):
    return b"".join(p.read_bytes() for p in store.path.parent.glob(store.path.name + "*"))


def test_an_erase_leaves_the_value_nowhere_once_every_device_moved(store, folder, keeper):
    relay = joined(keeper, folder)
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": SECRET}}])
    relay.log([event()])
    relay.sync()
    keeper.sync()
    old = keeper.key(1)
    assert any(SECRET in text for text in opened(folder, keeper, old))
    commands.erase(store, ME, "location", "example_gym")
    report = keeper.sync()
    assert report["new_epoch"] == {"epoch": 2, "reason": "erase"} and report["cleanup_waits_on"] == [PHONE]
    assert keeper.status()["cleanup_waits_on"] == [PHONE]
    relay.sync()  # takes key 2 and writes in epoch 2
    assert relay.epoch == 2 and relay.key != old
    report = keeper.sync()
    assert report["cleaned"] > 0 and keeper.key(1) is None and keeper.status()["cleanup_waits_on"] == []
    assert opened(folder, keeper, old) == []
    assert not any(SECRET in text for text in opened(folder, keeper, keeper.key(2)))
    one = "0000000001"
    assert not any(p.startswith((f"keys/{one}/", f"epochs/{one}", f"checkpoints/{one}/", f"plan/{one}-"))
                   for p in folder.list())
    assert SECRET.encode() not in raw(store)


def test_a_relay_drops_an_erased_record_it_still_holds(store, folder, keeper):
    relay = joined(keeper, folder)
    held = event()
    relay.log([held])
    relay.sync()
    keeper.sync()
    commands.erase(store, ME, "effort", held["record"]["id"])
    late = {**event(n=5), "record": held["record"], "id": "0193a0b0-0000-7000-8000-0000000000aa"}
    relay.log([late])
    keeper.sync()
    relay.sync()
    assert late["id"] not in relay.events


def test_an_erased_record_arriving_later_starts_a_new_epoch(store, folder, keeper):
    relay = joined(keeper, folder)
    held = event()
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "x"}}])
    commands.erase(store, ME, "location", "example_gym")
    keeper.sync()
    relay.sync()
    keeper.sync()
    assert keeper.epoch == 2
    stale = {**held, "record": {"kind": "location", "id": "example_gym"}, "payload": {"name": SECRET}}
    relay.log([stale])
    relay.plan = relay.plan_path = None  # as if it hadn't read the plan that lists the erase yet
    relay.events[stale["id"]] = stale
    relay._send(folder.list(), force=False)
    assert keeper.sync()["new_epoch"]["reason"] == "erase"


WEIGHT = {"samples": [{"source": "apple_health", "source_id": "S1", "start": "2026-11-23T07:00:00Z",
                       "type": "HKQuantityTypeIdentifierBodyMass", "value": 83.4567, "unit": "kg"}]}


def test_an_erased_sample_doesnt_come_back_under_the_new_key(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.send_samples(WEIGHT)
    relay.sync()
    keeper.sync()
    [found] = store.records("source_record")
    commands.erase(store, ME, "source_record", found["id"])  # before the phone saw the receipt
    assert keeper.sync()["new_epoch"]["reason"] == "erase"
    relay.sync()
    assert keeper.sync().get("cleaned")
    for _ in range(2):
        relay.sync()
        keeper.sync()
    assert not [t for t in opened(folder, keeper, keeper.key(keeper.epoch)) if "83.4567" in t]
    assert keeper.status()["cleanup_waits_on"] == []


def test_a_resent_erased_sample_starts_another_epoch(store, folder, keeper):
    relay = joined(keeper, folder)
    relay.send_samples(WEIGHT)
    relay.sync()
    keeper.sync()
    [found] = store.records("source_record")
    commands.erase(store, ME, "source_record", found["id"])
    keeper.sync()
    relay.send_samples(WEIGHT)  # Health hands the same sample over again
    relay.sync()
    assert keeper.sync()["new_epoch"]["reason"] == "erase"


def test_a_removed_device_gets_no_new_key_and_its_writes_are_ignored(store, folder, keeper):
    relay = joined(keeper, folder)
    assert keeper.remove(PHONE) == 2
    relay.sync()
    assert relay.removed and relay.key is None
    with pytest.raises(ByobotError):
        keeper.remove(PHONE)
    with pytest.raises(ByobotError):
        keeper.remove(keeper.device_id)
    sneaky = Relay(folder, store.store_id, PHONE, relay.keeper_sign, identity=relay.identity, epoch=1,
                   key=keeper.key(1), seq=50)
    sneaky.log([event()])
    sneaky._send(folder.list(), force=False)
    report = keeper.sync()
    assert "cleaned" in report  # no active device left to wait on
    assert not any(p.startswith(f"segments/{PHONE}/") for p in folder.list())


def test_a_removal_cut_short_can_be_run_again(store, folder, keeper):
    relay = joined(keeper, folder)
    create = folder.create

    def down(path, data):
        if path.startswith("epochs/"):
            raise ByobotError("can't reach Google Drive")
        create(path, data)

    folder.create = down
    with pytest.raises(ByobotError):
        keeper.remove(PHONE)
    folder.create = create
    assert keeper.remove(PHONE) == 2
    with pytest.raises(ByobotError):
        keeper.remove(PHONE)
    relay.sync()
    assert relay.removed and relay.key is None


def test_a_device_that_never_comes_back_holds_cleanup_until_removed(store, folder, keeper):
    gone = joined(keeper, folder)
    keeper.start_epoch()
    assert keeper.sync()["cleanup_waits_on"] == [PHONE]
    keeper.remove(gone.device_id)
    assert keeper.sync()["cleaned"] > 0 and {p[:9] for p in folder.list() if p.startswith("epochs/")} == {"epochs/00"}
    assert [p for p in folder.list() if p.startswith("epochs/")] == ["epochs/0000000003.json"]


def test_too_many_files_compacts_once_nothing_waits(store, folder, keeper, monkeypatch):
    monkeypatch.setattr(keeper_module, "MAX_FILES", 8)
    relay = joined(keeper, folder)
    for i in range(6):
        relay.log([event(n=i)])
        relay.sync()
    assert keeper.sync()["new_epoch"]["reason"] == "compact"
    assert "new_epoch" not in keeper.sync()  # waits on the relay, doesn't loop
    relay.sync()
    keeper.sync()
    assert keeper.epoch == 2 and len(folder.list()) <= 8


def test_a_wiped_folder_is_seeded_again_and_relays_send_again(store, folder, keeper):
    relay = joined(keeper, folder)
    sent = event()
    relay.log([sent])
    relay.sync()
    folder.files.clear()
    assert "reseeded" not in keeper.sync()  # a listing can lag behind another process; look again later
    keeper.clock.now += keeper_module.MISSING_S
    assert keeper.sync()["reseeded"] == 2
    relay.sync()
    keeper.sync()
    assert store.event(sent["id"]) and relay.epoch == 2
    relay.sync()
    assert relay.events == {}


def test_a_late_segment_from_a_retired_epoch_is_sent_again(store, folder, keeper):
    relay = joined(keeper, folder)
    keeper.start_epoch()
    relay.sync()
    keeper.sync()
    assert keeper.key(1) is None
    late = event()
    old = Relay(folder, store.store_id, PHONE, relay.keeper_sign, identity=relay.identity, epoch=1,
                key=b"\0" * 32, seq=90)
    old.log([late])
    old._send(folder.list(), force=False)
    report = keeper.sync()
    assert report["skipped"][0]["reason"] == "from an epoch whose key this keeper no longer holds"
    relay.log([late])
    relay.sync()
    keeper.sync()
    assert store.event(late["id"])


def test_recovery_rebuilds_the_store_from_the_folder_alone(store, folder, keeper, tmp_path, clock):
    code = keeper.recovery_code
    relay = joined(keeper, folder)
    sent = event()
    relay.log([sent])
    relay.sync()
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    keeper.sync()
    keeper.start_epoch()
    keeper.sync()
    wanted = {e["id"] for e in store.events()}
    fresh = Store(tmp_path / "new-mac.db")
    with pytest.raises(ByobotError, match="recovery code"):
        Keeper.recover(fresh, folder, recovery_code(), clock)
    again = Keeper.recover(fresh, folder, code, clock)
    assert {e["id"] for e in fresh.events()} >= wanted and fresh.store_id == store.store_id
    assert again.device_id == keeper.device_id == store.device_id == fresh.device_id and again.epoch == 3
    later = event(n=9)
    relay.log([later])
    relay.sync()
    again.sync()
    assert fresh.event(later["id"])


def test_recovery_keeps_a_phone_that_joined_since_the_epoch_began(store, folder, keeper, tmp_path, clock):
    code = keeper.recovery_code
    relay = joined(keeper, folder)  # only its key file names it until the next epoch
    sent = event()
    relay.log([sent])
    relay.sync()
    fresh = Store(tmp_path / "new-mac.db")
    again = Keeper.recover(fresh, folder, code, clock)
    relay.sync()
    again.sync()
    assert [d["id"] for d in again.devices() if d["class"] == "relay"] == [PHONE]
    assert not relay.removed and fresh.event(sent["id"])


def test_recovery_needs_the_code_at_setup(store, folder, tmp_path, clock):
    Keeper.setup(store, folder, clock)
    with pytest.raises(ByobotError, match="no recovery key"):
        Keeper.recover(Store(tmp_path / "new-mac.db"), folder, "A" * 32, clock)


def test_recovery_refuses_another_store(store, folder, keeper, tmp_path, clock):
    other = Store(tmp_path / "other.db")
    commands.log(other, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "x"}}])
    with pytest.raises(ByobotError, match="isn't the one"):
        Keeper.recover(other, folder, keeper.recovery_code, clock)


def test_status_says_where_the_folder_stands(store, folder, keeper):
    joined(keeper, folder, OTHER)
    status = keeper.status()
    assert status["epoch"] == 1 and status["recovery"] and status["cleanup_waits_on"] == []
    assert [d["class"] for d in status["devices"]] == ["keeper", "relay"]


def test_a_start_cut_short_whose_key_file_the_folder_doesnt_list_yet_runs_again(store, folder, keeper):
    relay = joined(keeper, folder)
    create, listed, hidden = folder.create, folder.list, set()

    def lost(path, data):  # the file lands and the answer doesn't
        create(path, data)
        if path.startswith("keys/"):
            hidden.add(path)
            raise ByobotError("can't reach Google Drive")

    folder.create = lost
    folder.list = lambda: {p: c for p, c in listed().items() if p not in hidden}
    with pytest.raises(ByobotError):
        keeper.start_epoch()
    folder.create = create
    keeper.sync()
    relay.sync()
    assert relay.epoch == keeper.epoch == 2 and relay.key == keeper.key(2)


def test_a_rotation_hands_over_to_new_keys_and_phones_follow(store, folder, keeper):
    relay = joined(keeper, folder)
    old = keeper.identity
    n = keeper.rotate()
    new = keeper.identity.public()["sign"]
    assert new != old.public()["sign"] and keeper.device(keeper.device_id)["sign"] == new
    relay.sync()
    assert relay.epoch == n and relay.keeper_sign == new and relay.plan is not None
    sent = event()
    relay.log([sent])
    relay.sync()
    keeper.sync()
    assert store.event(sent["id"])
    devices = [{"device_id": d["id"], "device_class": d["class"], "box": d["box"], "sign": d["sign"]}
               for d in keeper.devices()]
    folder.create(files.epoch_path(n + 1), files.epoch_file(old, store.store_id, n + 1, keeper.device_id, devices, 0))
    folder.create(files.epoch_path(n + 2), files.epoch_file(Identity.generate(), store.store_id, n + 2,
                                                            keeper.device_id, devices, 0, previous=old))
    relay.sync()
    assert relay.epoch == n and relay.keeper_sign == new  # the old keys can't take trust back


def test_a_phone_away_for_two_rotations_follows_both(store, folder, keeper):
    relay = joined(keeper, folder)
    keeper.rotate()
    n = keeper.rotate()
    relay.sync()
    assert relay.epoch == n and relay.keeper_sign == keeper.identity.public()["sign"]


def test_a_rotation_cut_short_still_hands_over_from_the_keys_phones_trust(store, folder, keeper, monkeypatch):
    relay = joined(keeper, folder)
    create = folder.create

    def lost(path, data):
        if path.startswith("epochs/"):
            raise OSError("offline")
        create(path, data)

    monkeypatch.setattr(folder, "create", lost)
    for _ in range(2):
        with pytest.raises(OSError):
            keeper.rotate()
    monkeypatch.setattr(folder, "create", create)
    keeper.sync()
    relay.sync()
    assert relay.epoch == keeper.epoch and relay.keeper_sign == keeper.identity.public()["sign"]
    assert keeper._previous() is None


def test_a_new_recovery_code_retires_the_old_one(store, folder, keeper, tmp_path, clock):
    old = keeper.recovery_code
    n = keeper.rotate(recovery=True)
    assert keeper.recovery_code not in (None, old)
    assert [p for p in folder.list() if p.endswith("/recovery.key")] == [files.recovery_path(n)]
    with pytest.raises(ByobotError, match="recovery code"):
        Keeper.recover(Store(tmp_path / "a.db"), folder, old, clock)
    again = Keeper.recover(Store(tmp_path / "b.db"), folder, keeper.recovery_code, clock)
    assert again.identity.public() == keeper.identity.public()


def moved(keeper, relay):
    """The relay writes in the keeper's epoch, so cleanup can run."""
    relay.sync()
    relay.log([event()])
    relay.sync()
    keeper.sync()
    keeper.sync()


def test_the_code_from_setup_still_opens_the_folder_after_a_rotation_and_cleanup(store, folder, keeper, tmp_path,
                                                                                 clock):
    code = keeper.recovery_code
    relay = joined(keeper, folder)
    keeper.sync()
    n = keeper.rotate()
    moved(keeper, relay)
    assert [p for p in folder.list() if p.endswith("/recovery.key")] == [files.recovery_path(n)]
    again = Keeper.recover(Store(tmp_path / "a.db"), folder, code, clock)
    assert again.epoch > n


def test_a_new_code_cut_short_is_kept_and_shown_by_the_retry(store, folder, keeper, tmp_path, clock, monkeypatch):
    relay = joined(keeper, folder)
    keeper.sync()
    create = folder.create

    def lost(path, data):
        if path.startswith("epochs/"):
            raise OSError("offline")
        create(path, data)

    monkeypatch.setattr(folder, "create", lost)
    with pytest.raises(OSError):
        keeper.rotate(recovery=True)
    made = keeper.unshown_code()
    assert keeper.local_status()["recovery_code"].startswith("not shown yet")
    monkeypatch.setattr(folder, "create", create)
    keeper.rotate()  # without --recovery, it still finishes the code the cut-short try made
    assert keeper.recovery_code == made
    moved(keeper, relay)
    Keeper.recover(Store(tmp_path / "a.db"), folder, made, clock)


def test_a_code_a_newer_one_replaced_is_refused_while_its_files_remain(store, folder, keeper, tmp_path, clock,
                                                                      monkeypatch):
    old = keeper.recovery_code
    monkeypatch.setattr(folder, "delete", lambda path: None)  # the old code's recovery keys stay
    keeper.rotate(recovery=True)
    with pytest.raises(ByobotError, match="newer recovery code"):
        Keeper.recover(Store(tmp_path / "a.db"), folder, old, clock)


def test_cleanup_waits_on_a_phone_that_cant_follow_a_handover(store, folder, keeper, monkeypatch):
    relay = joined(keeper, folder)
    keeper.sync()
    relay.sync()
    old = relay.epoch
    n = keeper.rotate()
    follow = Relay._follow
    monkeypatch.setattr(Relay, "_follow", lambda self, listing: {})
    sent = event()
    relay.log([sent])
    relay.sync()
    assert keeper.sync()["cleanup_waits_on"] == [PHONE]
    assert store.event(sent["id"]) and relay.epoch == old < n
    assert files.epoch_path(old) in folder.list() and keeper.key(old)
    monkeypatch.setattr(Relay, "_follow", follow)
    moved(keeper, relay)
    assert relay.epoch == n and files.epoch_path(old) not in folder.list()
