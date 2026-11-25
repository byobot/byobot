"""``byobot remote``: OAuth for remote MCP clients, and MCP over HTTP."""

import base64
import hashlib
import secrets

import pytest

from byobot import remote
from byobot.remote import Auth, OAuthError

BASE = "https://example-laptop.example.ts.net"
CALLBACK = "https://client.example/callback"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@pytest.fixture
def auth(byobot_home):
    return Auth(byobot_home / "remote" / "oauth.json", BASE + "/mcp")


def approved(auth, method="none"):
    client = auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                            "token_endpoint_auth_method": method})
    verifier, challenge = pkce()
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    tokens = auth.exchange(auth.authenticate(client["client_id"], client.get("client_secret")), code, CALLBACK, verifier)
    return client, tokens


# -- the authorization server ---------------------------------------------------

@pytest.mark.parametrize("url,expect", [
    ("https://example-laptop.example.ts.net/", "https://example-laptop.example.ts.net"),
    ("https://Example.TS.net:8443", "https://example.ts.net:8443"),
    ("http://localhost:8780", "http://localhost:8780"),
])
def test_check_url_accepts(url, expect):
    assert remote.check_url(url) == expect


@pytest.mark.parametrize("url", ["http://example.test", "https://example.test/mcp", "https://example.test/?a=1",
                                 "ftp://example.test", "example.test"])
def test_check_url_refuses(url):
    with pytest.raises(remote.ValidationFailed):
        remote.check_url(url)


def test_register_refuses_unsafe_redirects(auth):
    for uris in ([], ["http://client.example/cb"], ["https://client.example/cb#x"], "https://client.example/cb"):
        with pytest.raises(OAuthError) as exc:
            auth.register({"redirect_uris": uris, "token_endpoint_auth_method": "none"})
        assert exc.value.error == "invalid_redirect_uri"
    assert auth.register({"redirect_uris": ["http://localhost:6274/cb"], "token_endpoint_auth_method": "none"})


def test_a_public_client_gets_tokens_with_pkce(auth):
    client, tokens = approved(auth)
    assert "client_secret" not in client
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "byobot"
    assert auth.client_for_token(tokens["access_token"])["client_name"] == "Example Assistant"
    assert auth.clients() == [{"client_id": client["client_id"], "client_name": "Example Assistant"}]


def test_a_confidential_client_must_send_its_secret(auth):
    client = auth.register({"redirect_uris": [CALLBACK]})
    assert client["token_endpoint_auth_method"] == "client_secret_basic" and client["client_secret"]
    with pytest.raises(OAuthError) as exc:
        auth.authenticate(client["client_id"], "wrong")
    assert exc.value.status == 401
    assert auth.authenticate(client["client_id"], client["client_secret"])


def test_codes_are_single_use_and_need_the_right_verifier(auth):
    client = auth.register({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    with pytest.raises(OAuthError, match="does not match"):
        auth.exchange(client, code, CALLBACK, secrets.token_urlsafe(48))
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    with pytest.raises(OAuthError, match="redirect_uri"):
        auth.exchange(client, code, "https://client.example/other", verifier)
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    assert auth.exchange(client, code, CALLBACK, verifier)
    with pytest.raises(OAuthError) as exc:
        auth.exchange(client, code, CALLBACK, verifier)
    assert exc.value.error == "invalid_grant"


def test_a_code_works_only_for_its_client(auth):
    one = auth.register({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    two = auth.register({"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = auth.grant(one["client_id"], CALLBACK, challenge)
    with pytest.raises(OAuthError):
        auth.exchange(two, code, CALLBACK, verifier)


def test_refresh_tokens_rotate(auth):
    client, tokens = approved(auth)
    fresh = auth.refresh(client, tokens["refresh_token"])
    assert fresh["access_token"] != tokens["access_token"]
    with pytest.raises(OAuthError):
        auth.refresh(client, tokens["refresh_token"])
    assert auth.refresh(client, fresh["refresh_token"])


def test_access_tokens_expire(auth, monkeypatch):
    _, tokens = approved(auth)
    later = remote.time.time() + remote.ACCESS_TTL_S + 1
    monkeypatch.setattr(remote.time, "time", lambda: later)
    assert auth.client_for_token(tokens["access_token"]) is None


def test_resource_must_be_this_server(auth):
    auth.check_resource(None)
    auth.check_resource(BASE + "/mcp")
    auth.check_resource(BASE + "/")
    with pytest.raises(OAuthError) as exc:
        auth.check_resource("https://other.example/mcp")
    assert exc.value.error == "invalid_target"


def test_revoke_all_is_seen_by_a_running_server(auth, byobot_home):
    _, tokens = approved(auth)
    other = Auth(auth.path)  # as `byobot remote --revoke` would, from another process
    assert other.revoke_all() == 1
    assert auth.client_for_token(tokens["access_token"]) is None
    assert auth.clients() == []


def test_token_file_is_private_and_holds_only_hashes(auth):
    client, tokens = approved(auth, method="client_secret_post")
    text = auth.path.read_text()
    assert tokens["access_token"] not in text and tokens["refresh_token"] not in text
    assert client["client_secret"] not in text
    if remote.os.name == "posix":
        assert auth.path.stat().st_mode & 0o777 == 0o600


def test_profiles_do_not_share_tokens(byobot_home, monkeypatch):
    _, tokens = approved(Auth(remote_dir_for(monkeypatch, "alex") / "oauth.json"))
    assert Auth(remote_dir_for(monkeypatch, "sam") / "oauth.json").client_for_token(tokens["access_token"]) is None


def remote_dir_for(monkeypatch, name):
    monkeypatch.setenv("BYOBOT_PROFILE", name)
    return remote.remote_dir()
