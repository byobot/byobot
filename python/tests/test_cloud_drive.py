"""The cloud folder in Drive's hidden app folder, against a fake Drive: the folder rules, retries, the change feed,
and a keeper killed mid-upload and mid-epoch that recovers on its next run."""

import time
from collections import Counter

import pytest

from byobot import oauth
from byobot.cloud import drive as drive_module, drive, files
from byobot.cloud.drive import Drive, DriveFolder, NotConnected, UpstreamError
from byobot.cloud.folder import Exists
from byobot.cloud.keeper import Keeper
from byobot.core import commands
from byobot.core.store import Store
from byobot.util import ByobotError

from cloud_doubles import MemoryFolder, Relay
from fake_drive import FakeDrive, Killed
from test_cloud_epochs import SECRET, opened
from test_cloud_keeper import ME, PHONE, Clock, event

STORE = "0193a0b0-0000-7000-8000-00000000c001"


@pytest.fixture
def fake():
    return FakeDrive()


def signed_in():
    return {"client_id": "cid", "client_secret": "secret", "refresh_token": "refresh-1", "access_token": "access-1",
            "expires_at": time.time() + 3600}


def folder_on(fake, tmp_path, store_id=STORE, who="mac", naps=None):
    drive = Drive(fake, sleep=(naps.append if naps is not None else lambda s: None), stored=signed_in())
    return DriveFolder(store_id, drive, journal=tmp_path / f"{who}-journal.json")


def test_a_file_is_written_once_read_back_and_deleted_outright(fake, tmp_path):
    folder = folder_on(fake, tmp_path)
    folder.create("devices/abc.json", b"one")
    with pytest.raises(Exists):
        folder.create("devices/abc.json", b"two")
    assert folder.read("devices/abc.json") == b"one" and folder.read("devices/none.json") is None
    assert list(folder.list()) == ["devices/abc.json"] and fake.names() == [f"{STORE}/devices/abc.json"]
    folder.delete("devices/abc.json")
    folder.delete("devices/abc.json")
    assert folder.list() == {} and fake.files == {}
    with pytest.raises(ValueError):
        folder.create("../escape", b"x")


def test_each_store_sees_only_its_own_files_page_by_page(fake, tmp_path):
    mine, other = folder_on(fake, tmp_path), folder_on(fake, tmp_path, "0193a0b0-0000-7000-8000-00000000c002", "b")
    for n in range(7):
        mine.create(f"devices/d{n}.json", b"x")
    other.create("devices/d0.json", b"y")
    assert len(folder_on(fake, tmp_path, who="fresh").list()) == 7
    assert list(other.list()) == ["devices/d0.json"]


def test_another_device_s_files_show_through_the_change_feed(fake, tmp_path):
    mac, phone = folder_on(fake, tmp_path), folder_on(fake, tmp_path, who="phone")
    mac.list()
    assert mac.changed() is False
    for n in range(5):
        phone.create(f"segments/p/{n}.seg", b"set")
    calls = len(fake.calls)
    assert mac.changed() is True and len(mac.list()) == 5
    assert "GET /drive/v3/files" not in fake.calls[calls:]  # the feed, not a full listing
    phone.delete("segments/p/0.seg")
    assert "segments/p/0.seg" not in mac.list()


def test_a_full_listing_comes_every_ten_minutes_whatever_the_feed_says(fake, tmp_path):
    mac = folder_on(fake, tmp_path)
    mac.list()
    calls = len(fake.calls)
    mac.list()
    assert "GET /drive/v3/files" not in fake.calls[calls:]
    mac._loaded_at -= drive_module.RELIST
    calls = len(fake.calls)
    mac.list()
    assert "GET /drive/v3/files" in fake.calls[calls:]


@pytest.mark.parametrize("code", [429, 500, 503])
def test_a_busy_drive_is_retried_with_backoff(fake, tmp_path, code):
    naps = []
    folder = folder_on(fake, tmp_path, naps=naps)
    fake.fail("GET /drive/v3/files", code, times=2)
    folder.create("devices/abc.json", b"one")
    assert len(naps) == 2 and naps[1] > naps[0] and list(folder.list()) == ["devices/abc.json"]


def test_drive_down_for_good_fails_the_call(fake, tmp_path):
    folder = folder_on(fake, tmp_path)
    fake.fail("GET /drive/v3/changes/startPageToken", 503, times=drive_module.TRIES)
    with pytest.raises(UpstreamError, match="503"):
        folder.list()


def test_an_expired_token_is_refreshed_and_saved(fake, tmp_path):
    from byobot import credentials

    folder = folder_on(fake, tmp_path)
    fake.token = "access-elsewhere"  # Google expired it early
    folder.create("devices/abc.json", b"one")
    assert fake.refreshes == 1 and credentials.load("google")["access_token"] == fake.token


def test_a_revoked_sign_in_says_to_sign_in_again(fake, tmp_path, monkeypatch):
    def refused(provider, form, opener):
        raise NotConnected("Google signed byobot out of Drive; run `byobot cloud setup` to sign in again")

    folder = folder_on(fake, tmp_path)
    folder.drive.stored["expires_at"] = 0
    monkeypatch.setattr(oauth, "post_token", refused)
    with pytest.raises(NotConnected, match="cloud setup"):
        folder.list()
    with pytest.raises(NotConnected):
        Drive(fake)  # nothing saved


def test_an_upload_whose_answer_is_lost_lands_once(fake, tmp_path):
    folder = folder_on(fake, tmp_path)
    fake.fail("POST /upload", "lost")
    folder.create("devices/abc.json", b"one")
    assert fake.names() == [f"{STORE}/devices/abc.json"] and folder.read("devices/abc.json") == b"one"


def test_an_upload_cut_short_by_a_crash_is_finished_with_the_same_id(fake, tmp_path):
    fake.fail("POST /upload", "kill")
    with pytest.raises(Killed):
        folder_on(fake, tmp_path).create("devices/abc.json", b"one")
    fake.lagging = set(fake.files)
    folder_on(fake, tmp_path).create("devices/abc.json", b"one")  # the next run, before listings show the file
    assert fake.names() == [f"{STORE}/devices/abc.json"]
    fake.lagging.clear()
    assert folder_on(fake, tmp_path, who="fresh").read("devices/abc.json") == b"one"


def test_a_damaged_upload_is_deleted_and_sent_again(fake, tmp_path):
    folder = folder_on(fake, tmp_path)
    fake.fail("POST /upload", "damage")
    with pytest.raises(UpstreamError, match="damaged"):
        folder.create("devices/abc.json", b"one")
    assert folder.list() == {}
    folder.create("devices/abc.json", b"one")
    assert folder.read("devices/abc.json") == b"one"


# -- a keeper and a phone over Drive ------------------------------------------------------------------------------


class Mac:
    """The keeper as a process: each run opens the store and Drive afresh, as a new `byobot` command would."""

    def __init__(self, fake, tmp_path, path):
        self.fake, self.tmp_path, self.path = fake, tmp_path, path

    def run(self, act):
        with Store(self.path) as store:
            keeper = Keeper(store, folder_on(self.fake, self.tmp_path, store.store_id))
            return act(store, keeper)


def setup(fake, tmp_path, store):
    keeper = Keeper.setup(store, folder_on(fake, tmp_path, store.store_id), Clock(), recovery=True)
    phone_folder = folder_on(fake, tmp_path, store.store_id, who="phone")
    relay = Relay.join(phone_folder, keeper.join_code()["link"], PHONE)
    keeper.sync()
    relay.sync()
    return keeper, relay


def settled(fake, mac, relay):
    for _ in range(3):
        relay.sync()
        mac.run(lambda s, k: k.sync())
    assert relay.events == {} and relay.sent == {}
    names = Counter(fake.names())
    assert all(n == 1 for n in names.values()), [n for n, c in names.items() if c > 1]


def test_a_week_of_sets_goes_both_ways_over_drive(fake, tmp_path, store):
    keeper, relay = setup(fake, tmp_path, store)
    sets = [event(n=n) for n in range(30)]
    relay.log(sets)
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Gym"}}])
    relay.sync()
    report = keeper.sync()
    assert report["published"] >= 1 and all(store.event(e["id"]) for e in sets)
    relay.sync()
    assert relay.plan is not None and relay.events == {}


@pytest.mark.parametrize("nth", range(2))
def test_a_keeper_killed_mid_upload_recovers_on_its_next_run(fake, tmp_path, store, nth):
    keeper, relay = setup(fake, tmp_path, store)
    sets = [event(n=n) for n in range(5)]
    relay.log(sets)
    relay.sync()
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Gym"}}])
    fake.fail("POST /upload", "pass", times=nth)
    fake.faults.append(("POST /upload", "kill"))
    with pytest.raises(Killed):
        keeper.sync()
    fake.faults.clear()
    mac = Mac(fake, tmp_path, store.path)
    settled(fake, mac, relay)
    held = mac.run(lambda s, k: {e["id"] for e in s.events()})
    assert {e["id"] for e in sets} <= held
    assert mac.run(lambda s, k: s.events(since=k.published())) == []


@pytest.mark.parametrize("nth", range(5))
def test_a_keeper_killed_mid_epoch_finishes_it_on_its_next_run(fake, tmp_path, store, nth):
    keeper, relay = setup(fake, tmp_path, store)
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": SECRET}}])
    keeper.sync()
    old = keeper.key(1)
    commands.erase(store, ME, "location", "example_gym")
    fake.fail("POST /upload", "pass", times=nth)
    fake.faults.append(("POST /upload", "kill"))
    with pytest.raises(Killed):
        keeper.sync()
    fake.faults.clear()
    mac = Mac(fake, tmp_path, store.path)
    settled(fake, mac, relay)
    epoch = mac.run(lambda s, k: k.epoch)
    assert epoch >= 2 and relay.epoch == epoch
    assert mac.run(lambda s, k: k.status()["cleanup_waits_on"]) == []
    folder = folder_on(fake, tmp_path, store.store_id, who="check")
    assert all(int(p.split("/")[1][:10]) == epoch for p in folder.list() if p.startswith(("epochs/", "keys/")))
    assert mac.run(lambda s, k: opened(folder, k, old)) == []
    assert not any(SECRET in text for text in mac.run(lambda s, k: opened(folder, k, k.key(epoch))))


def test_a_keeper_killed_mid_cleanup_finishes_it_on_its_next_run(fake, tmp_path, store):
    keeper, relay = setup(fake, tmp_path, store)
    keeper.start_epoch()
    relay.sync()
    fake.fail("DELETE /drive/v3/files/", "pass", times=2)
    fake.faults.append(("DELETE /drive/v3/files/", "kill"))
    with pytest.raises(Killed):
        keeper.sync()
    fake.faults.clear()
    mac = Mac(fake, tmp_path, store.path)
    settled(fake, mac, relay)
    folder = folder_on(fake, tmp_path, store.store_id, who="check")
    assert not any(p.startswith(("epochs/0000000001", "keys/0000000001/", "checkpoints/0000000001/"))
                   for p in folder.list())


def test_a_phone_upload_that_fails_keeps_its_samples_for_the_next_sync(fake, tmp_path, store):
    keeper, relay = setup(fake, tmp_path, store)
    relay.log([event()])
    relay.send_samples({"samples": [{"source": "apple_health", "source_id": "W1",
                                     "type": "HKQuantityTypeIdentifierBodyMass", "start": "2026-11-23T06:30:00-05:00",
                                     "end": "2026-11-23T06:30:00-05:00", "value": 80.0, "unit": "kg"}]})
    fake.fail("POST /upload", 503, times=drive_module.TRIES)
    with pytest.raises(UpstreamError):
        relay.sync()
    settled(fake, Mac(fake, tmp_path, store.path), relay)
    assert [r["payload"]["source_id"] for r in store.records("source_record")] == ["W1"]


def test_a_start_retried_while_drive_still_hides_its_leftovers_seals_the_same_key(fake, tmp_path, store,
                                                                                    monkeypatch):
    keeper = Keeper.setup(store, folder_on(fake, tmp_path, store.store_id, "mac1"), Clock())
    relay = Relay.join(folder_on(fake, tmp_path, store.store_id, "phone"), keeper.join_code()["link"], PHONE)
    keeper.sync()
    relay.sync()
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "x"}}])
    commands.erase(store, ME, "location", "example_gym")
    before = set(fake.files)
    real = Keeper._segment

    def die(self, kind, *rest):
        if kind == "checkpoint":
            raise Killed("power cut")
        return real(self, kind, *rest)

    monkeypatch.setattr(Keeper, "_segment", die)
    with pytest.raises(Killed):
        keeper.sync()  # epoch 2's key files are up, then the Mac dies
    monkeypatch.setattr(Keeper, "_segment", real)
    fake.lagging = set(fake.files) - before
    again = Keeper(Store(store.path), folder_on(fake, tmp_path, store.store_id, "mac2"), Clock())
    assert again.sync()["reseeded"] == 2
    fake.lagging = set()
    sent = event()
    relay.log([sent])
    for _ in range(3):
        relay.sync()
        again.sync()
    assert relay.key == again.key(2) and again.store.event(sent["id"])


def test_a_listing_behind_another_process_starts_no_extra_epoch(fake, tmp_path, store):
    a = Keeper.setup(store, folder_on(fake, tmp_path, store.store_id, "a"), Clock())
    b = Keeper(Store(store.path), folder_on(fake, tmp_path, store.store_id, "b"), Clock())
    b.sync()
    before = set(fake.files)
    assert a.start_epoch() == 2  # a new epoch from another process
    fake.lagging = set(fake.files) - before
    assert "reseeded" not in b.sync()
    fake.lagging = set()
    assert "reseeded" not in b.sync() and b.epoch == 2


@pytest.mark.parametrize("name", ["devices/a b.json", "keys/0000000001/x y.key"])
def test_a_file_byobot_never_writes_is_left_alone(fake, tmp_path, store, name):
    keeper = Keeper.setup(store, folder_on(fake, tmp_path, store.store_id), Clock())
    keeper.join_code()
    fake.files["planted"] = {"id": "planted", "name": f"{store.store_id}/{name}",
                             "appProperties": {"byobot_store": store.store_id}, "parents": ["appDataFolder"],
                             "data": b"{}"}
    fake.changes.append("planted")
    keeper.start_epoch()
    keeper.sync()
    assert keeper.forget() > 0


def test_a_planted_seq_doesnt_use_up_the_keepers(store):
    from cloud_doubles import MemoryFolder

    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock())
    folder.files["plan/0000000001-9999999999.seg"] = b"anything"
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "x"}}])
    keeper.sync()
    folder.files.pop("plan/0000000001-9999999999.seg", None)
    commands.log(store, ME, [{"kind": "location", "id": "example_pool", "payload": {"name": "y"}}])
    assert keeper.sync()["published"] == 1


def test_a_long_lived_client_never_writes_back_an_older_sign_in(fake):
    from byobot import credentials

    old = {"client_id": "old-client", "client_secret": "s", "refresh_token": "refresh-1", "access_token": "a",
           "expires_at": time.time() - 10}
    credentials.save("google", old)
    serve = Drive(fake, sleep=lambda s: None)  # `serve` keeps this one
    credentials.save("google", {**old, "client_id": "new-client", "refresh_token": "refresh-2",
                                "expires_at": time.time() + 3600})  # `cloud setup` again in a terminal
    with pytest.raises(UpstreamError, match="sign-in changed"):
        serve.call("GET", "https://www.googleapis.com/drive/v3/changes/startPageToken")
    assert credentials.load("google")["client_id"] == "new-client"


WEIGHT = "HKQuantityTypeIdentifierBodyMass"


def sample(source_id):
    return {"source": "apple_health", "source_id": source_id, "type": WEIGHT,
            "start": "2026-11-23T06:30:00-05:00", "end": "2026-11-23T06:30:00-05:00", "value": 80.0, "unit": "kg"}


def test_a_health_reading_deleted_after_a_lost_upload_stays_deleted(store, tmp_path):
    fake = FakeDrive()
    keeper, relay = setup(fake, tmp_path, store)
    relay.send_samples({"samples": [sample("W1")]})
    fake.fail("POST /upload", "lost")  # the segment lands, its answer is lost, and Drive stays down
    fake.fail("POST /upload", 503, times=drive_module.TRIES - 1)
    with pytest.raises(UpstreamError):
        relay.sync()
    first = [i for i, f in fake.files.items() if f"/segments/{PHONE}/" in f["name"]]
    assert len(first) == 1
    relay.send_samples({"samples": [], "deleted": [{"source": "apple_health", "source_id": "W1", "type": WEIGHT}]})
    relay.sync()
    mac = Mac(fake, tmp_path, store.path)
    fake.lagging = set(first)  # a fresh listing that doesn't show the older segment yet
    mac.run(lambda s, k: k.sync())
    fake.lagging = set()
    for _ in range(3):
        relay.sync()
        mac.run(lambda s, k: k.sync())
    assert "W1" not in {r["payload"]["source_id"] for r in store.records("source_record")}, \
        "a reading deleted in Health came back"


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
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
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


def test_a_close_an_older_byobot_began_still_finishes(store):
    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock())
    n = keeper.epoch + 1
    with store.transaction():
        keeper.state.set("closing", n)
    me = [{"device_id": keeper.device_id, "device_class": "keeper", **keeper.identity.public()}]
    folder.create(files.epoch_path(n), files.epoch_file(keeper.identity, store.store_id, n, keeper.device_id, me, 0))
    keeper.forget()
    assert list(folder.list()) == [files.epoch_path(n)] and keeper.epoch == n
