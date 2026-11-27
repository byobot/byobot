"""The `byobot cloud` commands over a fake Drive, and Google's sign-in from the Desktop client."""

import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from byobot import commands, credentials
from byobot.cloud import drive, keeper as keeper_module, sync
from byobot.cloud.drive import Drive, DriveFolder
from byobot.commands import Context, run
from byobot.core import commands as core
from byobot.link import pairing_link
from byobot.util import ByobotError, ValidationFailed

from cloud_doubles import Relay
from fake_drive import FakeDrive, Response
from test_cloud_drive import signed_in
from test_cloud_keeper import PHONE, event

CLI = {"kind": "human", "id": "cli"}


@pytest.fixture
def fake():
    return FakeDrive()


@pytest.fixture
def cloud(fake, monkeypatch, tmp_path):
    """A terminal, a sign-in that lands at once, and Drive faked."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(sync, "drive_folder", lambda store, quick=False: DriveFolder(
        store.store_id, Drive(fake, sleep=lambda s: None), journal=tmp_path / f"{store.store_id}.json"))

    def sign_in(client, **_):
        credentials.save(drive.NAME, {**signed_in(), **client})
        return {"signed_in": True}

    monkeypatch.setattr(drive, "sign_in", sign_in)
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid.apps.example", "client_secret": "s"}}))
    return client


def test_setup_join_sync_status_remove_and_compact(cloud, fake, tmp_path):
    ctx = Context(actor=CLI)
    assert run("cloud status", ctx, {}) == {"on": False}
    out = run("cloud setup", ctx, {"client_file": str(cloud), "ios_client_id": "ios.apps.example"})
    assert out["epoch"] == 1 and len(out["recovery_code"]) > 10
    assert credentials.load("google")["ios_client_id"] == "ios.apps.example"
    join = run("cloud join", ctx, {"wait": False})
    assert join["link"].startswith("byobot://pair?v=1&") and "client=ios.apps.example" in join["link"]
    phone = Relay.join(DriveFolder(ctx.store().store_id, Drive(fake), journal=tmp_path / "phone.json"),
                       join["link"], PHONE)
    assert run("cloud sync", ctx, {})["join"] == "admitted"
    phone.sync()
    phone.log([event()])
    phone.sync()
    assert run("cloud sync", ctx, {})["read"] == 1
    status = run("cloud status", ctx, {})
    assert status["on"] and [d["class"] for d in status["devices"]] == ["keeper", "relay"]
    assert run("cloud remove", ctx, {"device_id": PHONE})["epoch"] == 2
    assert run("cloud compact", ctx, {})["epoch"] == 3
    rotated = run("cloud rotate", ctx, {"recovery": True})
    assert rotated["epoch"] == 4 and rotated["recovery_code"].startswith("A")
    assert "recovery_code" not in run("cloud status", ctx, {})
    again = run("cloud setup", ctx, {})
    assert again["keeper"] == "already" and "recovery_code" not in again
    ctx.close()


def test_join_waits_until_the_phone_joins(cloud, fake, tmp_path, monkeypatch):
    ctx = Context(actor=CLI)
    run("cloud setup", ctx, {"client_file": str(cloud), "ios_client_id": ""})
    store = ctx.store()

    def phone_scans(seconds):
        if not any(f"/devices/{PHONE}" in n for n in fake.names()):
            secret = json.loads(store.conn.execute("SELECT value FROM cloud_meta WHERE key = 'join'").fetchone()[0])
            keeper = store.conn.execute("SELECT sign FROM devices WHERE class = 'keeper'").fetchone()[0]
            link = pairing_link(store_id=store.store_id, secret=secret["secret"], keeper_key=keeper)
            Relay.join(DriveFolder(store.store_id, Drive(fake), journal=tmp_path / "phone.json"), link, PHONE)

    monkeypatch.setattr(commands.time, "sleep", phone_scans)
    out = run("cloud join", ctx, {})
    assert out["join"] == "admitted" and out["device"] == PHONE and "client=" not in out["link"]
    ctx.close()


def test_setup_needs_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(ValidationFailed, match="in a terminal"):
        run("cloud setup", Context(actor=CLI), {})


def test_commands_before_setup_say_to_run_it(cloud):
    with pytest.raises(ValidationFailed, match="cloud setup"):
        run("cloud sync", Context(actor=CLI), {})


def test_a_new_computer_recovers_with_the_code(cloud, fake, tmp_path):
    first = Context(actor=CLI)
    code = run("cloud setup", first, {"client_file": str(cloud), "ios_client_id": ""})["recovery_code"]
    core.log(first.store(), CLI, [{"kind": "location", "id": "example_gym", "payload": {"name": "Gym"}}])
    run("cloud sync", first, {})
    first.close()
    second = Context(actor=CLI, path=tmp_path / "new-mac" / "byobot.db")
    with pytest.raises(ByobotError, match="cloud recover"):
        run("cloud setup", second, {})
    second.close()
    second = Context(actor=CLI, path=tmp_path / "new-mac-2" / "byobot.db")
    out = run("cloud recover", second, {"code": code})
    assert out["recovered"] and second.store().record("location", "example_gym")
    second.close()


# -- Google's sign-in ------------------------------------------------------------------------------------------------


class Google:
    """Google's token endpoint, and a browser that signs in and follows the redirect to byobot's loopback page."""

    def __init__(self, answer):
        self.answer, self.asked, self.form = answer, None, None

    def browser(self, url):
        self.asked = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        back = self.asked["redirect_uri"] + "/?" + urllib.parse.urlencode({"code": "code-1",
                                                                           "state": self.asked["state"]})
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=5).read(), daemon=True).start()

    def __call__(self, req):
        self.form = dict(urllib.parse.parse_qsl(req.data.decode()))
        return Response(json.dumps(self.answer).encode())


GRANT = {"access_token": "a", "refresh_token": "r", "expires_in": 3599, "scope": drive.SCOPE}
CLIENT = {"client_id": "cid", "client_secret": "s", "ios_client_id": "ios"}


def test_sign_in_uses_pkce_and_a_loopback_page_and_keeps_the_tokens():
    google = Google(GRANT)
    assert drive.sign_in(CLIENT, opener=google, open_browser=google.browser)["signed_in"]
    assert google.asked["scope"] == drive.SCOPE and google.asked["code_challenge_method"] == "S256"
    assert google.asked["redirect_uri"].startswith("http://127.0.0.1:")
    assert google.form["code"] == "code-1" and len(google.form["code_verifier"]) >= 43
    saved = credentials.load("google")
    assert saved["refresh_token"] == "r" and saved["ios_client_id"] == "ios" and saved["expires_at"] > time.time()


def test_sign_in_refuses_a_project_still_in_testing():
    google = Google({**GRANT, "refresh_token_expires_in": 604799})
    with pytest.raises(ByobotError, match="In production"):
        drive.sign_in(CLIENT, opener=google, open_browser=google.browser)
    assert credentials.load("google") is None


def test_sign_in_refuses_more_than_the_app_folder():
    google = Google({**GRANT, "scope": f"{drive.SCOPE} https://www.googleapis.com/auth/drive"})
    with pytest.raises(ByobotError, match="only"):
        drive.sign_in(CLIENT, opener=google, open_browser=google.browser)


def test_a_client_file_must_be_a_desktop_client(tmp_path):
    path = tmp_path / "web.json"
    path.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(ByobotError, match="Desktop app"):
        drive.read_client(path)


def test_setup_again_with_another_google_account_doesnt_strand_the_phone(monkeypatch, tmp_path):
    first, other = FakeDrive(), FakeDrive()
    account = {"drive": first}
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(sync, "drive_folder", lambda store, quick=False: DriveFolder(
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


def joined(cloud, fake, tmp_path):  # noqa: F811
    from byobot.cloud.drive import Drive, DriveFolder
    from cloud_doubles import Relay

    ctx = Context(actor=CLI)
    run("cloud setup", ctx, {"client_file": str(cloud), "ios_client_id": "ios.apps.example"})
    join = run("cloud join", ctx, {"wait": False})
    Relay.join(DriveFolder(ctx.store().store_id, Drive(fake), journal=tmp_path / "phone.json"), join["link"], PHONE)
    run("cloud sync", ctx, {})
    return ctx


def test_removing_a_phone_keeps_google_access_unless_asked(cloud, fake, tmp_path, monkeypatch):  # noqa: F811
    asked = []
    monkeypatch.setattr("byobot.commands._ask", lambda prompt: asked.append(prompt) or "")
    out = run("cloud remove", joined(cloud, fake, tmp_path), {"device_id": PHONE})
    assert out["google_access"] == "kept" and "every phone" in out["note"] and "every phone" in asked[0]


def test_revoking_google_access_says_every_phone_signs_out(cloud, fake, tmp_path):  # noqa: F811
    out = run("cloud remove", joined(cloud, fake, tmp_path), {"device_id": PHONE, "revoke_google": True})
    assert out["google_access"] == "revoke" and "cloud join" in out["next"]
