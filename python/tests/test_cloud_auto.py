"""The Mac syncs Drive on its own: once a minute while `serve`, `remote` or `mcp` runs, one process at a time, never
inside a read; and `cloud off`."""

import time

import pytest

from byobot.cloud import sync
from byobot.cloud.drive import Drive, DriveFolder, UpstreamError
from byobot.cloud.keeper import Keeper, due, tried
from byobot.cloud.lock import held
from byobot.cloud.sync import CloudPoller, autosync
from byobot.commands import Context, run
from byobot.util import ValidationFailed

from cloud_doubles import MemoryFolder, Relay
from test_cloud_commands import CLI, cloud, fake  # noqa: F401 - fixtures
from test_cloud_keeper import PHONE, Clock, event


def meta(ctx, key):
    row = ctx.store().conn.execute("SELECT value FROM cloud_meta WHERE key = ?", (key,)).fetchone()
    return row and row[0]


def age(ctx):
    """Pretend the last sync was long ago."""
    ctx.store().conn.execute("UPDATE cloud_meta SET value = '0' WHERE key = 'tried_at'")


def efforts(ctx):
    return ctx.store().conn.execute("SELECT count(*) FROM events WHERE kind = 'effort'"
                                    ).fetchone()[0]


@pytest.fixture
def phone(cloud, fake, tmp_path):  # noqa: F811
    """A keeper with one joined phone."""
    ctx = Context(actor=CLI)
    run("cloud setup", ctx, {"client_file": str(cloud), "ios_client_id": ""})
    link = run("cloud join", ctx, {"wait": False})["link"]
    relay = Relay.join(DriveFolder(ctx.store().store_id, Drive(fake), journal=tmp_path / "phone.json"), link, PHONE)
    run("cloud sync", ctx, {})
    relay.sync()
    yield ctx, relay
    ctx.close()


def test_a_read_leaves_drive_alone(phone, monkeypatch):
    ctx, relay = phone
    relay.log([event()])
    relay.sync()
    age(ctx)
    monkeypatch.setattr(sync, "autosync", lambda *a, **k: pytest.fail("a read synced Drive"))
    run("status", ctx, {})
    run("list", ctx, {"kind": "effort"})
    assert efforts(ctx) == 0 and meta(ctx, "tried_at") == "0"


def test_the_poller_syncs_at_most_once_a_minute(phone):
    ctx, relay = phone
    poller = CloudPoller(ctx.path, [].append)
    age(ctx)
    poller.poll()
    relay.log([event()])
    relay.sync()
    assert poller.poll() is None
    assert efforts(ctx) == 0
    age(ctx)
    poller.poll()
    assert efforts(ctx) == 1


def test_the_poller_goes_on_when_drive_fails(phone, monkeypatch):
    ctx, _ = phone

    def unreachable(store, quick=False):
        raise UpstreamError("can't reach Google Drive: offline")

    sync._folders.clear()
    monkeypatch.setattr(sync, "drive_folder", unreachable)
    age(ctx)
    assert CloudPoller(ctx.path, [].append).poll() == {"error": "can't reach Google Drive: offline"}
    assert run("status", ctx, {})["counts"]
    assert meta(ctx, "error") == "can't reach Google Drive: offline"


def test_the_poller_leaves_a_missing_store_uncreated(tmp_path):
    db = tmp_path / "none.db"
    assert CloudPoller(db, [].append).poll() is None
    assert not db.exists()


def test_a_read_skips_drive_while_another_process_syncs(phone):
    ctx, relay = phone
    relay.log([event()])
    relay.sync()
    age(ctx)
    with held(sync.lock_path(ctx.store())) as mine:
        assert mine
        assert sync.autosync(ctx.store()) is None
    assert efforts(ctx) == 0 and meta(ctx, "tried_at") == "0"


def test_a_store_without_drive_sync_is_left_as_it_is(tmp_path):
    ctx = Context(actor=CLI)
    run("init", ctx, {})
    run("status", ctx, {})
    assert not ctx.store().conn.execute("SELECT 1 FROM cloud_meta").fetchone()
    ctx.close()


def test_serve_syncs_once_a_minute_and_says_what_came_in(phone):
    ctx, relay = phone
    lines = []
    poller = CloudPoller(ctx.path, lines.append)
    relay.log([event()])
    relay.sync()
    age(ctx)
    assert poller.poll()["read"] == 1
    assert lines == ["Drive sync: read 1 file from phones"]
    assert poller.poll() is None  # not due again yet


def test_serve_says_once_when_drive_fails_and_when_it_works_again(phone, monkeypatch):
    ctx, _ = phone
    lines = []
    poller = CloudPoller(ctx.path, lines.append)
    real = sync.drive_folder

    def unreachable(store, quick=False):
        raise UpstreamError("your Google Drive is full, so byobot can't upload; free some space")

    sync._folders.clear()
    monkeypatch.setattr(sync, "drive_folder", unreachable)
    for _ in range(2):
        age(ctx)
        poller.poll()
    monkeypatch.setattr(sync, "drive_folder", real)
    age(ctx)
    poller.poll()
    assert lines == ["Drive sync: your Google Drive is full, so byobot can't upload; free some space",
                     "Drive sync: working again"]


def test_cloud_off_stops_sync_until_setup(phone):
    ctx, relay = phone
    assert run("cloud off", ctx, {}) == {"on": False, "deleted": 0}
    relay.log([event()])
    relay.sync()
    age(ctx)
    assert CloudPoller(ctx.path, [].append).poll() is None
    assert efforts(ctx) == 0
    with pytest.raises(ValidationFailed, match="turn it on again"):
        run("cloud sync", ctx, {})
    assert run("cloud setup", ctx, {})["read"] == 1
    assert efforts(ctx) == 1


def test_cloud_off_can_delete_the_folder(phone, fake):
    ctx, relay = phone
    store_id = ctx.store().store_id
    relay.log([event()])
    relay.sync()
    assert run("cloud off", ctx, {"delete": True})["deleted"] > 0
    left = [n for n in fake.names() if n.startswith(store_id + "/")]
    assert left == [f"{store_id}/epochs/0000000002.json"]  # only the notice that lists no phone
    relay.log([event(n=2)])
    assert relay.sync() == {"removed": True, "joined": False}
    assert [n for n in fake.names() if n.startswith(store_id + "/")] == left
    status = run("cloud status", ctx, {})
    assert status["off"] and [d["class"] for d in status["devices"]] == ["keeper"]
    assert ctx.store().counts()  # the store is untouched
    assert run("cloud setup", ctx, {})["epoch"] == 3
    assert relay.sync() == {"removed": True}


def test_a_read_during_recover_finds_the_keeper_busy(store, tmp_path, monkeypatch):
    from cloud_doubles import MemoryFolder
    from byobot.cloud.keeper import Keeper
    from byobot.core.store import Store
    from test_cloud_keeper import Clock, joined

    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock(), recovery=True)
    joined(keeper, folder)
    fresh = Store(tmp_path / "new-mac.db")
    seen = []
    real = Keeper._read

    def read(self, listing):  # an agent's read while recover is still reading the folder
        if not seen:
            seen.append(sync.autosync(Store(fresh.path), folder=folder))
        return real(self, listing)

    monkeypatch.setattr(Keeper, "_read", read)
    with sync.lock(fresh):  # as `cloud recover` holds it, under the new store's old id
        Keeper.recover(fresh, folder, keeper.recovery_code, Clock())
    assert seen == [None]


def test_background_sync_runs_again_once_a_clock_that_ran_ahead_is_set_right(store):
    folder = MemoryFolder()
    Keeper.setup(store, folder, Clock(time.time()))
    tried(store, clock=lambda: time.time() + 2 * 86400)  # a sync while the clock ran two days ahead
    assert due(store, 60), "no background sync for the next two days"
    assert autosync(store, folder) is not None
