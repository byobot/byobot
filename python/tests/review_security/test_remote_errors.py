"""Review: an unexpected error's text, which can hold file paths with the account name or SQL, went back to remote
callers."""

import json

from byobot import mcp
from byobot.remote import Auth

from test_remote import BASE, request, running  # noqa: F401


def test_an_unexpected_error_keeps_its_text_off_the_network(running, byobot_home, monkeypatch):  # noqa: F811
    key = Auth(byobot_home / "remote" / "oauth.json", BASE + "/mcp").create_key("assistant")

    def broken(*args, **kwargs):
        raise RuntimeError("/home/example-user/.byobot/byobot.db is locked")

    monkeypatch.setattr(mcp, "run", broken)
    resp, raw = request(running, "POST", "/api/v1/tools/status", {}, {"Authorization": f"Bearer {key}"})
    assert resp.status == 500 and b"example-user" not in raw
    assert "internal error" in json.loads(raw)["message"]
