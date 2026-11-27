"""Review: running `cloud setup` again ("run it again to sign in again") and picking another Google account in the
browser silently moves the keeper to an empty folder in that account. The keeper seeds a new epoch there, while the
phone stays signed in to the first account: it keeps writing there and shows Drive sync as working, its sets never
reach the Mac through Drive again, and nothing deletes or re-keys the first account's folder (so a later erase never
reaches it)."""

import json

import pytest

from byobot import commands, credentials
from byobot.cloud import drive
from byobot.cloud import keeper as keeper_module
from byobot.cloud.drive import Drive, DriveFolder
from byobot.cloud.relay import Relay
from byobot.commands import Context, run
from byobot.util import ByobotError

from fake_drive import FakeDrive
from test_cloud_drive import signed_in
from test_cloud_keeper import PHONE, event

CLI = {"kind": "human", "id": "cli"}


def test_setup_again_with_another_google_account_doesnt_strand_the_phone(monkeypatch, tmp_path):
    first, other = FakeDrive(), FakeDrive()
    account = {"drive": first}
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(commands, "_drive_folder", lambda store, quick=False: DriveFolder(
        store.store_id, Drive(account["drive"], sleep=lambda s: None), journal=tmp_path / "mac.json"))
    monkeypatch.setattr(drive, "sign_in", lambda client, **_: credentials.save(drive.NAME, {**signed_in(), **client}))
    monkeypatch.setattr(keeper_module, "MISSING_S", 0)
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid.apps.example", "client_secret": "s"}}))
    ctx = Context(actor=CLI)
    run("cloud setup", ctx, {"client_file": str(client), "ios_client_id": "ios.apps.example"})
    link = run("cloud join", ctx, {"wait": False})["link"]
    phone = Relay.join(DriveFolder(ctx.store().store_id, Drive(first, sleep=lambda s: None),
                                   journal=tmp_path / "phone.json"), link, PHONE)
    run("cloud sync", ctx, {})
    phone.sync()

    account["drive"] = other  # the browser was signed in to another Google account this time
    try:
        run("cloud setup", ctx, {})
    except ByobotError:
        return  # refusing to move the logbook to another account silently is a fix
    run("cloud sync", ctx, {})
    run("cloud sync", ctx, {})
    sent = event()
    phone.log([sent])
    for _ in range(3):
        report = phone.sync()
        run("cloud sync", ctx, {})
    assert report.get("epoch") is not None  # the phone still thinks Drive sync works
    assert ctx.store().event(sent["id"]), "the phone's set never reaches the Mac through Drive"
