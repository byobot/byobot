"""Review: `byobot remote` counted only OAuth clients as connected, so with only API keys every start opened approval
on the public tunnel."""

from byobot import remote
from byobot.remote import Auth

from test_remote import BASE


def test_approval_stays_closed_when_an_api_key_is_connected(byobot_home):
    Auth(byobot_home / "remote" / "oauth.json", BASE + "/mcp").create_key("assistant")
    server = remote.make_server(BASE, "127.0.0.1", 0)
    try:
        assert server.pairing is None
    finally:
        server.server_close()
