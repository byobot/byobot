"""The keeper and a relay over a local folder: joining, sending both ways, the plan, and what gets refused."""

import json
import sqlite3

import pytest

from byobot.cloud import files, keeper as keeper_module, segment
from byobot.cloud.crypto import CloudError
from byobot.cloud.drive import UpstreamError
from byobot.cloud.folder import Exists
from byobot.cloud.keeper import JOIN_TTL_S, Keeper
from byobot.core import commands, hlc
from byobot.core.store import Store
from byobot.link import pairing_link
from byobot.util import ByobotError, uuid7

from cloud_doubles import LocalFolder, MemoryFolder, Relay, parse_join
from fake_drive import Killed

PHONE = "0193a0b0-0000-7000-8000-00000000ab01"
OTHER = "0193a0b0-0000-7000-8000-00000000ab02"
ME = {"kind": "human", "id": "me"}


def event(device_id=PHONE, actor=ME, n=0, kind="effort"):
    return {"id": uuid7(), "schema_version": 1, "record": {"kind": kind, "id": uuid7()}, "op": "put",
            "kind_version": 1, "hlc": hlc.format((1795000000000 + 1000 * n, 0)), "ts": "2026-11-23T08:00:00-05:00",
            "device_id": device_id, "actor": actor, "prev": [],
            "payload": {"activity_id": "barbell_back_squat",
                        "time": {"start": "2026-11-23T07:40:00-05:00"}}}


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

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
    assert keeper.device_id == store.device_id  # the Mac has one id, on the hub and in Drive
    listing = folder.list()
    assert files.epoch_path(1) in listing and any(p.startswith("plan/0000000001-") for p in listing)
    with pytest.raises(ByobotError, match="already"):
        Keeper.setup(store, folder)
    with pytest.raises(ByobotError, match="already has a keeper"):
        from byobot.core.store import Store
        Keeper.setup(Store(store.path.parent / "other.db"), folder)


def test_a_join_code_holds_no_key(keeper):
    code = keeper.join_code()
    assert parse_join(code["link"]) == {"store_id": code["store_id"], "secret": code["secret"],
                                        "keeper_key": code["keeper_key"]}
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
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
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
    forged = pairing_link(store_id=code["store_id"], secret="A" * 22 + "==", keeper_key=code["keeper_key"])
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
    code = keeper.join_code()
    link = pairing_link(store_id=code["store_id"], secret=code["secret"], keeper_key=impostor.public()["sign"])
    with pytest.raises(ByobotError, match="doesn't hold that Mac's logbook"):
        Relay.join(folder, link, PHONE)


def test_a_phone_the_keeper_never_let_in_can_answer_a_new_code(store, folder, keeper):
    Relay.join(folder, keeper.join_code()["link"], PHONE)
    keeper.clock.now += 3600
    assert keeper.sync()["join"] == "expired"
    assert files.device_path(PHONE) not in folder.list()  # the keeper clears what no one admitted
    folder.create(files.device_path(PHONE), b"left by an earlier code")
    relay = Relay.join(folder, keeper.join_code()["link"], PHONE)
    assert keeper.sync()["join"] == "admitted"
    relay.sync()
    assert relay.key is not None
    with pytest.raises(Exists):  # a phone already in
        Relay.join(folder, keeper.join_code()["link"], PHONE)


def test_a_local_folder_never_replaces_a_file(tmp_path):
    folder = LocalFolder(tmp_path, "0193a0b0-0000-7000-8000-00000000c001")
    folder.create("plan/0000000001-0000000001.seg", b"one")
    with pytest.raises(Exists):
        folder.create("plan/0000000001-0000000001.seg", b"two")
    assert folder.read("plan/0000000001-0000000001.seg") == b"one"
    assert list(folder.list()) == ["plan/0000000001-0000000001.seg"]
    for bad in ("../x", "a//b", "/abs", "a/../b"):
        with pytest.raises(ValueError):
            folder.create(bad, b"")
    folder.delete("plan/0000000001-0000000001.seg")
    folder.delete("plan/0000000001-0000000001.seg")
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
        segment.read(b"junk", path="plan/0000000001-0000000001.seg", store_id=store.store_id, key=keeper.key(1),
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
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "x"}}])
    folder.blocked.clear()
    keeper.sync()
    keeper.start_epoch()
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


def test_an_older_keeper_keeps_a_newer_phone_s_segment_until_it_is_upgraded(store, folder, keeper, monkeypatch):
    from byobot.cloud import keeper as keeper_module

    relay = joined(keeper, folder)
    sent = event()
    relay.log([sent])
    monkeypatch.setattr(segment, "FORMAT", segment.FORMAT + 1)  # the phone runs a newer byobot
    relay.sync()
    monkeypatch.undo()
    path = next(p for p in folder.list() if p.startswith("segments/"))
    assert "newer byobot" in keeper.sync()["skipped"][0]["reason"]
    assert path in folder.list() and "upgrade" in keeper.local_status()
    keeper.start_epoch()
    relay.sync()  # the phone moves to the new epoch, so only the newer segment holds the old one back
    assert keeper.sync().get("cleanup_waits_on") == ["an upgrade of byobot on this Mac"] and path in folder.list()
    monkeypatch.setattr(segment, "FORMAT", segment.FORMAT + 1)  # this Mac is upgraded
    monkeypatch.setattr(keeper_module, "__version__", "next")
    keeper.sync()
    assert store.event(sent["id"]) and "upgrade" not in keeper.local_status()


def test_a_rejoined_phone_starts_with_no_receipts_from_before(store, folder, keeper):
    relay = joined(keeper, folder)
    joined(keeper, folder, OTHER)  # it hasn't moved, so the first epoch's files stay
    relay.log([event()])
    relay.sync()
    keeper.sync()
    keeper.remove(PHONE)
    again = Relay.join(folder, keeper.join_code()["link"], PHONE, identity=relay.identity)
    assert keeper.sync()["join"] == "admitted"
    assert keeper.receipts()[PHONE]["read"] == []  # its new relay counts seqs from 1 again
    assert not [p for p in folder.list() if p.startswith(f"segments/{PHONE}/0001-")]
    sent = event(n=1)
    again.sync()
    again.log([sent])
    again.sync()
    keeper.sync()
    again.sync()
    assert store.event(sent["id"]) and not again.events


def test_a_phone_on_the_hub_and_in_drive_is_one_device(store, folder, keeper):
    store.pair(PHONE, "Example iPhone", "ab" * 32)
    relay = joined(keeper, folder)
    relay.log([event()])
    relay.sync()
    keeper.sync()
    assert store.rows("SELECT id, name, class FROM devices ORDER BY class") == [
        {"id": store.device_id, "name": None, "class": "keeper"}, {"id": PHONE, "name": "Example iPhone",
                                                                    "class": "relay"}]
    phone = keeper.local_status()["devices"][1]
    assert phone["id"] == PHONE and phone["last_heard"] == store.peer(PHONE)["last_seen_at"]
    store.unpair(PHONE)
    assert keeper.device(PHONE)["removed"] == 0
    keeper.forget()
    assert store.peer(PHONE)["revoked_at"] and keeper.device(PHONE) is None
    assert [d["id"] for d in keeper.devices()] == [store.device_id]


def test_an_event_read_late_from_a_retired_epoch_stays_in_the_folder(store, tmp_path):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock, recovery=True)
    code = keeper.recovery_code
    relay = joined(keeper, folder)
    sent = event()
    relay.log([sent])
    relay.sync()  # a segment in epoch 1
    keeper.start_epoch()  # a new epoch reads nothing first: the checkpoint of epoch 2 lacks the phone's set
    keeper.sync()  # reads the epoch-1 segment late; the plan's receipt lists it
    relay.sync()  # takes key 2, sees its segment read, forgets the set, writes in epoch 2
    assert relay.epoch == 2 and relay.events == {}
    keeper.sync()  # every device moved: epoch 1's files, the set's segment among them, are deleted
    assert store.event(sent["id"])
    assert not any(p.startswith("segments/") and "/0000000001-" in p for p in folder.list())

    fresh = Store(tmp_path / "new-mac.db")
    Keeper.recover(fresh, folder, code, clock)
    assert fresh.event(sent["id"]), "the phone's set is in no file of the folder, so recovery lost it"


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


def backup(store, path):
    target = sqlite3.connect(path)
    store.conn.backup(target)
    target.close()


def test_a_keeper_restored_from_an_older_backup_still_hears_the_phone(store, tmp_path):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    relay = joined(keeper, folder)
    backup(store, tmp_path / "backup.db")  # last night's backup: epoch 1
    for _ in range(2):  # two compactions (or erases) since then; each time the phone moves and epoch n-1 goes
        keeper.start_epoch()
        relay.sync()
        keeper.sync()
    assert keeper.epoch == relay.epoch == 3 and keeper.key(1) is None and keeper.key(2) is None
    store.close()

    restored = Keeper(Store(tmp_path / "backup.db"), folder, clock)  # the Mac comes back from the backup
    restored.sync()
    clock.now += keeper_module.MISSING_S
    restored.sync()  # its epoch file is gone from the folder, so it seeds the folder again
    sent = event()
    relay.log([sent])
    for _ in range(4):
        relay.sync()
        restored.sync()
    assert restored.store.event(sent["id"]), "the phone's set never reaches the restored Mac through Drive"
    assert relay.events == {}


def test_recover_gets_every_event_or_says_it_cant(store, tmp_path, monkeypatch):
    monkeypatch.setattr(segment, "MAX_BODY", 128 * 1024)
    for n in range(40):
        commands.log(store, ME, [{"kind": "location", "id": f"example_gym_{n}",
                                  "payload": {"name": f"Gym {n}", "notes": f"{n:04d}" + "x" * 3900}}])
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock, recovery=True)
    fresh = Store(tmp_path / "new-mac.db")
    try:
        Keeper.recover(fresh, folder, keeper.recovery_code, clock)
    except Exception:
        return  # saying it can't is fine
    assert len(fresh.records("location")) == 40, "recover reported success with part of the logbook"


def test_recover_says_so_when_a_checkpoint_part_is_missing(store, tmp_path):
    for n in range(3):
        commands.log(store, ME, [{"kind": "location", "id": f"example_gym_{n}", "payload": {"name": f"Gym {n}"}}])
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock, recovery=True)
    folder.delete(segment.name("checkpoint", keeper.epoch, 0))
    fresh = Store(tmp_path / "new-mac.db")
    mine = fresh.device_id
    with pytest.raises(ByobotError, match="can't be read"):
        Keeper.recover(fresh, folder, keeper.recovery_code, clock)
    assert not fresh.conn.execute("SELECT 1 FROM cloud_meta").fetchone()  # it can run again
    assert fresh.device_id == mine


def _cut_short(store):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    phone = Relay.join(folder, keeper.join_code()["link"], PHONE)
    key_path = files.key_path(keeper.epoch, PHONE)
    create, read = folder.create, folder.read

    def lost(path, data):
        create(path, data)
        if path == key_path:
            raise ByobotError("the answer to the key file's upload was lost")

    def down(path):
        if path == key_path:
            raise ByobotError("can't reach Google Drive")
        return read(path)

    folder.create, folder.read = lost, down
    with pytest.raises(ByobotError):
        keeper.sync()  # `cloud join` keeps syncing until the phone is in; the network drops
    folder.create, folder.read = create, read
    return keeper, phone, clock


def test_a_phone_that_took_its_key_gets_its_events_to_the_mac(store):
    keeper, phone, clock = _cut_short(store)
    clock.now += JOIN_TTL_S + 1  # back online after the code ran out
    keeper.sync()
    sent = event(PHONE, n=1)
    phone.log([sent])
    for _ in range(3):
        phone.sync()
        keeper.sync()
    assert phone.key is None or any(e["id"] == sent["id"] for e in store.events()), \
        f"the phone counts itself in; the keeper says {keeper.last_join()} and skips what it writes"


def test_a_new_code_first_lets_in_the_phone_that_took_its_key(store):
    keeper, phone, _ = _cut_short(store)
    keeper.join_code()
    phone.sync()
    assert phone.key is not None and keeper.device(PHONE)


def test_a_code_shown_while_serve_ends_the_last_one_still_lets_the_phone_in(store):
    folder = MemoryFolder()
    clock = Clock()
    serve = Keeper.setup(store, folder, clock)
    serve.join_code()  # an earlier `cloud join` that nobody answered
    clock.now += JOIN_TTL_S + 1
    join = Keeper(Store(store.path), folder, Clock(clock.now))  # `byobot cloud join` in another process
    shown = {}

    def serve_clock():
        if not shown:  # `cloud join` writes its code just after serve's sync read the old one
            shown.update(join.join_code())
        return clock.now

    serve.clock = serve_clock
    serve.sync()
    serve.clock = clock
    assert json.loads(join.state.get("join") or "null"), "the code `cloud join` just showed was unset"
    phone = Relay.join(folder, shown["link"], PHONE)
    join.sync()
    assert join.last_join() == {"join": "admitted", "device": PHONE}
    phone.sync()
    assert phone.key is not None


def test_a_phone_still_moves_after_a_failed_compact_is_run_again(store, monkeypatch):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    phone = joined(keeper, folder, PHONE)
    real = Keeper._write

    def cut_short(self, path, data):
        real(self, path, data)
        if path == files.epoch_path(2):
            raise Killed("the run ended right after epoch 2's file landed")

    monkeypatch.setattr(Keeper, "_write", cut_short)
    with pytest.raises(Killed):
        keeper.start_epoch()
    monkeypatch.setattr(Keeper, "_write", real)
    create, calls = folder.create, []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 1:
            raise ByobotError("Google Drive answered 503")
        create(path, data)

    folder.create = flaky
    with pytest.raises(ByobotError):
        keeper.start_epoch()  # run again, since it failed; Drive fails once more partway
    folder.create = create
    for _ in range(3):
        keeper.sync()
        phone.sync()
    assert phone.epoch == keeper.epoch, f"the phone is stuck in epoch {phone.epoch}, the keeper in {keeper.epoch}"
    assert keeper.status()["cleanup_waits_on"] == []


def test_a_removed_phone_cant_read_what_the_keeper_writes_next(store, monkeypatch):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    lost = joined(keeper, folder, PHONE)
    real = Keeper._write

    def cut_short(self, path, data):
        real(self, path, data)
        if path == files.epoch_path(2):
            raise Killed("the run ended right after epoch 2's file landed")

    monkeypatch.setattr(Keeper, "_write", cut_short)
    with pytest.raises(Killed):
        keeper.start_epoch()
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
    from cloud_doubles import Relay
    from fake_drive import FakeDrive
    from test_cloud_drive import folder_on

    fake = FakeDrive()
    keeper = Keeper.setup(store, folder_on(fake, tmp_path, store.store_id), Clock())
    lost = Relay.join(folder_on(fake, tmp_path, store.store_id, who="phone"), keeper.join_code()["link"], PHONE)
    keeper.sync()
    lost.sync()
    fake.fail("POST /upload", "pass", times=2)  # epoch 2's key file and plan land
    fake.fail("POST /upload", "lost")  # the epoch file lands, its answer is lost, and Drive stays down
    fake.fail("POST /upload", 503, times=drive_module.TRIES - 1)
    with pytest.raises(UpstreamError):
        keeper.start_epoch()
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


def _off_delete_fails_partway(store):
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock)
    relay = joined(keeper, folder)
    real, calls = folder.delete, []

    def flaky(path):
        calls.append(path)
        if len(calls) == 3:
            raise UpstreamError("can't reach Google Drive: connection reset")
        real(path)

    folder.delete = flaky
    with pytest.raises(ByobotError):
        keeper.forget()  # `cloud off --delete` answers an error
    folder.delete = real
    return keeper, relay


def test_a_failed_off_delete_leaves_keeper_and_phone_agreeing(store):
    keeper, relay = _off_delete_fails_partway(store)
    for _ in range(2):  # the person runs `cloud sync`, or `serve` syncs on its own
        keeper.sync()
        relay.sync()
    assert keeper.is_off or (not relay.removed and relay.epoch == keeper.epoch), \
        "the phone was told it was taken off Drive sync while the keeper still counts on it"


def test_after_a_failed_off_delete_the_next_epoch_still_starts(store):
    keeper, relay = _off_delete_fails_partway(store)
    keeper.sync()
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Gym"}}])
    commands.erase(store, ME, "location", "example_gym")
    report = keeper.sync()  # an erase starts the next epoch, whose number the closed marker already took
    assert keeper.is_off or report.get("new_epoch"), report


def test_an_older_phone_still_takes_a_plan_that_carries_a_newer_message(store, monkeypatch):
    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock())
    relay = joined(keeper, folder)
    real = Keeper._segment

    def newer_keeper(self, kind, seq, messages, epoch=None):
        if kind == "plan":  # a later byobot adds a message to the plan segment
            messages = [*messages, {"reminders": {"items": []}}]
        return real(self, kind, seq, messages, epoch)

    monkeypatch.setattr(Keeper, "_segment", newer_keeper)
    sent = event(relay.device_id, n=1)
    relay.log([sent])
    relay.sync()
    keeper.sync()
    assert store.event(sent["id"])
    relay.sync()
    assert relay.events == {} and relay.sent == {}, "the phone never learns the Mac read its set"
