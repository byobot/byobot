"""The Mac syncs Drive on its own: before reads, once a minute while `serve` runs, one process at a time; and
`cloud off`."""

import pytest

from byobot import commands
from byobot.cloud.drive import Drive, DriveFolder, UpstreamError
from byobot.cloud.lock import held
from byobot.cloud.relay import Relay
from byobot.commands import Context, run
from byobot.serve import CloudPoller
from byobot.util import ValidationFailed, home

from test_cloud_commands import CLI, cloud, fake  # noqa: F401 - fixtures
from test_cloud_keeper import PHONE, event


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
    link = run("cloud join", ctx, {"wait": "no"})["link"]
    relay = Relay.join(DriveFolder(ctx.store().store_id, Drive(fake), journal=tmp_path / "phone.json"), link, PHONE)
    run("cloud sync", ctx, {})
    relay.sync()
    yield ctx, relay
    ctx.close()


def test_a_read_brings_in_what_the_phone_sent(phone):
    ctx, relay = phone
    relay.log([event()])
    relay.sync()
    age(ctx)
    run("status", ctx, {})
    assert efforts(ctx) == 1 and meta(ctx, "error") is None and meta(ctx, "synced_at")


def test_reads_sync_at_most_once_a_minute(phone):
    ctx, relay = phone
    age(ctx)
    run("status", ctx, {})
    relay.log([event()])
    relay.sync()
    run("status", ctx, {})
    assert efforts(ctx) == 0
    age(ctx)
    run("status", ctx, {})
    assert efforts(ctx) == 1


def test_a_read_goes_on_when_drive_fails(phone, monkeypatch):
    ctx, _ = phone

    def unreachable(store, quick=False):
        raise UpstreamError("can't reach Google Drive: offline")

    commands._folders.clear()
    monkeypatch.setattr(commands, "_drive_folder", unreachable)
    age(ctx)
    assert run("status", ctx, {})["counts"]
    assert meta(ctx, "error") == "can't reach Google Drive: offline"


def test_a_read_skips_drive_while_another_process_syncs(phone):
    ctx, relay = phone
    relay.log([event()])
    relay.sync()
    age(ctx)
    with held(home() / "cloud" / f"keeper-{ctx.store().store_id}.lock") as mine:
        assert mine
        assert commands.autosync(ctx.store()) is None
    assert efforts(ctx) == 0 and meta(ctx, "tried_at") == "0"


def test_a_store_without_drive_sync_is_left_as_it_is(tmp_path):
    ctx = Context(actor=CLI)
    run("init", ctx, {})
    run("status", ctx, {})
    assert not ctx.store().conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'cloud_meta'").fetchone()
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
    real = commands._drive_folder

    def unreachable(store, quick=False):
        raise UpstreamError("your Google Drive is full, so byobot can't upload; free some space")

    commands._folders.clear()
    monkeypatch.setattr(commands, "_drive_folder", unreachable)
    for _ in range(2):
        age(ctx)
        poller.poll()
    monkeypatch.setattr(commands, "_drive_folder", real)
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
    run("status", ctx, {})
    assert efforts(ctx) == 0
    with pytest.raises(ValidationFailed, match="turn it on again"):
        run("cloud sync", ctx, {})
    assert run("cloud setup", ctx, {})["read"] == 1
    assert efforts(ctx) == 1


def test_cloud_off_can_delete_the_folder(phone, fake):
    ctx, _ = phone
    store_id = ctx.store().store_id
    assert run("cloud off", ctx, {"delete": "yes"})["deleted"] > 0
    assert not [n for n in fake.names() if n.startswith(store_id + "/")]
    assert run("cloud status", ctx, {}) == {"on": False}
    assert ctx.store().counts()  # the store is untouched
    assert run("cloud setup", ctx, {})["epoch"] == 1
