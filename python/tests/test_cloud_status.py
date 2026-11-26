"""`cloud status`: what a person needs to see when Drive sync goes quiet."""

from pathlib import Path

from byobot import commands
from byobot.cloud.drive import UpstreamError
from byobot.commands import run
from byobot.util import sync_folder_warning

from test_cloud_auto import age, phone  # noqa: F401 - fixtures
from test_cloud_commands import cloud, fake  # noqa: F401 - fixtures
from test_cloud_keeper import PHONE, event


def test_status_shows_when_each_device_was_last_heard_and_the_size(phone):  # noqa: F811
    ctx, relay = phone
    relay.log([event()])
    relay.sync()
    run("cloud sync", ctx, {})
    status = run("cloud status", ctx, {})
    keeper, phone_row = status["devices"]
    assert status["on"] and status["synced_at"] and "error" not in status
    assert keeper["last_heard"] == status["synced_at"]
    assert phone_row["id"] == PHONE and phone_row["last_heard"]
    assert status["size_bytes"] > 0 and status["files"] > 0


def test_status_says_what_went_wrong_and_still_shows_the_devices(phone, monkeypatch):  # noqa: F811
    ctx, _ = phone

    def full(store, quick=False):
        raise UpstreamError("your Google Drive is full, so byobot can't upload; free some space")

    commands._folders.clear()
    monkeypatch.setattr(commands, "_drive_folder", full)
    age(ctx)
    status = run("cloud status", ctx, {})
    assert status["on"] and status["error"].startswith("your Google Drive is full")
    assert [d["class"] for d in status["devices"]] == ["keeper", "relay"]


def test_status_when_off(phone):  # noqa: F811
    ctx, _ = phone
    run("cloud off", ctx, {})
    status = run("cloud status", ctx, {})
    assert status["on"] is False and status["off"] is True and status["devices"]


def test_a_home_inside_a_sync_folder_is_warned_about(tmp_path):
    for synced in (("Library", "CloudStorage", "GoogleDrive-x", "byobot"),
                   ("Library", "Mobile Documents", "com~apple~CloudDocs"), ("Dropbox", "byobot")):
        path = Path(tmp_path, *synced)
        assert sync_folder_warning(path), path.absolute().parts
    assert sync_folder_warning(Path(tmp_path) / ".byobot") is None


def test_status_warns_about_a_synced_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOBOT_HOME", str(tmp_path / "Dropbox" / "byobot"))
    ctx = commands.Context(actor={"kind": "human", "id": "cli"})
    assert "Dropbox" in run("status", ctx, {})["warning"]
    assert "Dropbox" in run("cloud status", ctx, {})["warning"]
    ctx.close()


def test_status_doesnt_wait_on_drive_again_right_after_a_failed_sync(phone, monkeypatch):  # noqa: F811
    ctx, _ = phone

    def unreachable(store, quick=False):
        raise UpstreamError("can't reach Google Drive: offline")

    commands._folders.clear()
    monkeypatch.setattr(commands, "_drive_folder", unreachable)
    age(ctx)
    assert run("cloud status", ctx, {})["error"] == "can't reach Google Drive: offline"

    def never(store, quick=False):
        raise AssertionError("asked Drive again")

    monkeypatch.setattr(commands, "_drive_folder", never)
    assert run("cloud status", ctx, {})["error"] == "can't reach Google Drive: offline"
