"""Review: `cloud remove` stops a phone reading new epochs, but its Google sign-in can still delete files in the
folder. Revoking that sign-in signs out every phone, so it's asked, and no unless asked for."""

from byobot.commands import Context, run

from test_cloud_commands import CLI, cloud, fake  # noqa: F401
from test_cloud_drive import signed_in  # noqa: F401
from test_cloud_keeper import PHONE


def joined(cloud, fake, tmp_path):  # noqa: F811
    from byobot.cloud.drive import Drive, DriveFolder
    from byobot.cloud.relay import Relay

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
