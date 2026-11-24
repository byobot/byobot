import io
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from byobot import credentials, whoop
from byobot.commands import Context, run
from byobot.util import ValidationFailed


@pytest.fixture(autouse=True)
def file_credentials(monkeypatch):
    monkeypatch.setenv("BYOBOT_CREDENTIALS", "file")


class Fake:
    """Answers like WHOOP, from canned answers per (method, path)."""

    def __init__(self, answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.requests = []

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        body = urllib.parse.parse_qs(req.data.decode()) if req.data else None
        self.requests.append((req.get_method(), url.path, urllib.parse.parse_qs(url.query), body,
                              dict(req.header_items())))
        answer = self.answers[(req.get_method(), url.path)].pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "error", {}, None)
        return io.BytesIO(json.dumps(answer).encode() if answer is not None else b"")


def tokens(n):
    return {"access_token": f"access-{n}", "refresh_token": f"refresh-{n}", "expires_in": 3600,
            "scope": whoop.SCOPES, "token_type": "bearer"}


def connected(**overrides):
    stored = {"client_id": "example-client", "client_secret": "example-secret", "access_token": "access-1",
              "refresh_token": "refresh-1", "expires_at": time.time() + 3600, **overrides}
    credentials.save(whoop.NAME, stored)
    return stored


def test_connect_signs_in_and_keeps_the_tokens():
    fake = Fake({("POST", "/oauth/oauth2/token"): [tokens(1)]})
    opened = []
    out = whoop.connect("example-client", "example-secret", opener=fake, open_browser=opened.append,
                        wait=lambda state: "example-code")
    assert out == {"connected": True, "scopes": whoop.SCOPES.split()}
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
    assert query["redirect_uri"] == whoop.REDIRECT and query["scope"] == whoop.SCOPES and len(query["state"]) == 8
    form = fake.requests[0][3]
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["example-code"]
    assert form["client_secret"] == ["example-secret"] and form["redirect_uri"] == [whoop.REDIRECT]
    assert credentials.load(whoop.NAME)["refresh_token"] == "refresh-1"


def test_the_redirect_is_caught_on_this_machine(monkeypatch):
    monkeypatch.setattr(whoop, "PORT", 0)
    port = {}
    real = whoop.http.server.HTTPServer

    class Server(real):
        def server_activate(self):
            super().server_activate()
            port["n"] = self.server_address[1]

    monkeypatch.setattr(whoop.http.server, "HTTPServer", Server)
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=whoop.wait_for_code("abcd1234", timeout=10)))
    thread.start()
    while "n" not in port:
        time.sleep(0.01)
    url = f"http://127.0.0.1:{port['n']}/whoop/callback?code=example-code&state=abcd1234"
    with urllib.request.urlopen(url) as resp:
        assert b"connected" in resp.read()
    thread.join()
    assert result["code"] == "example-code"


def test_an_expired_token_is_refreshed_and_the_new_one_kept():
    connected(expires_at=time.time() - 1)
    fake = Fake({("POST", "/oauth/oauth2/token"): [tokens(2)], ("GET", "/developer/v2/cycle"): [{"records": []}]})
    whoop.Client(fake).request("GET", "/v2/cycle", {"limit": 1})
    form = fake.requests[0][3]
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["refresh-1"]
    assert fake.requests[1][4]["Authorization"] == "Bearer access-2"
    assert credentials.load(whoop.NAME)["refresh_token"] == "refresh-2"


def test_a_refused_token_is_refreshed_once():
    connected()
    fake = Fake({("GET", "/developer/v2/cycle"): [401, {"records": []}], ("POST", "/oauth/oauth2/token"): [tokens(2)]})
    assert whoop.Client(fake).request("GET", "/v2/cycle") == {"records": []}


def test_not_connected_says_how_to_connect():
    with pytest.raises(whoop.NotConnected, match="byobot connect whoop"):
        whoop.Client(Fake({}))


def test_disconnect_revokes_and_forgets():
    connected()
    fake = Fake({("DELETE", "/developer/v2/user/access"): [None]})
    assert whoop.disconnect(fake) == {"disconnected": True, "revoked_at_whoop": True}
    assert credentials.load(whoop.NAME) is None


def test_connect_needs_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(ValidationFailed, match="terminal"):
        run("connect whoop", Context(actor={"kind": "human", "id": "cli"}), {})
