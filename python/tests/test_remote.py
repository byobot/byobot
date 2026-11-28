"""``byobot remote``: OAuth for remote MCP clients, and MCP over HTTP."""

import base64
import dataclasses
import hashlib
import http.client
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from byobot import commands, remote, web
from byobot.core import query as query_module
from byobot.core.store import Store
from byobot.remote import READ, Auth, OAuthError

from helpers import serving

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
    assert tokens["token_type"] == "Bearer" and tokens["scope"] == "read write"
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
        auth.refresh(client, tokens["refresh_token"])  # a replay, so the tokens that replaced it stop working
    assert auth.client_for_token(fresh["access_token"]) is None


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


def test_remove_one_assistant_and_leave_the_others(auth):
    client, tokens = approved(auth)
    key = auth.create_key("example_key", READ)
    listed = {a["route"]: a for a in auth.assistants()}
    assert listed["sign_in"]["id"] == client["client_id"] and listed["api_key"]["read_only"]
    other = Auth(auth.path)  # as `byobot device remove` would, from another process
    assert other.remove(client["client_id"])["route"] == "sign_in"
    assert auth.client_for_token(tokens["access_token"]) is None and auth.client_for_token(key)
    assert other.remove(listed["api_key"]["id"])["name"] == "example_key"
    assert auth.client_for_token(key) is None and auth.assistants() == []
    with pytest.raises(remote.NotFound):
        other.remove(client["client_id"])


def test_removing_an_assistant_voids_a_code_already_handed_out(auth):
    first, _ = approved(auth)
    client = auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                            "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    Auth(auth.path).remove(first["client_id"])
    with pytest.raises(OAuthError):
        auth.exchange(auth.authenticate(client["client_id"], None), code, CALLBACK, verifier)


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


# -- over HTTP ----------------------------------------------------------------

@pytest.fixture
def running(byobot_home):
    from byobot.core.store import Store

    Store(byobot_home / "byobot.db").close()
    with serving(remote.make_server(BASE, "127.0.0.1", 0, pair=True)) as server:
        yield server


def request(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    data = body if isinstance(body, (bytes, type(None))) else (
        urlencode(body).encode() if isinstance(body, dict) and (headers or {}).get("Content-Type", "").startswith(
            "application/x-www-form") else json.dumps(body).encode())
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp, raw


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def connect(server, name="Example Assistant", **approve):
    """Run the whole connector flow: register, approve with the code, exchange. Returns (client, tokens)."""
    resp, raw = request(server, "POST", "/oauth/register", {"client_name": name, "redirect_uris": [CALLBACK],
                                                            "token_endpoint_auth_method": "none"})
    assert resp.status == 201
    client = json.loads(raw)
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
              "resource": BASE + "/mcp"}
    resp, raw = request(server, "GET", "/oauth/authorize?" + urlencode(params))
    assert resp.status == 200 and name.encode() in raw and b"client.example" in raw
    resp, _ = request(server, "POST", "/oauth/authorize", {**params, "approval_code": server.pairing.code, **approve},
                      FORM)
    assert resp.status == 302
    location = urlsplit(resp.getheader("Location"))
    q = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == CALLBACK
    assert q["state"] == ["xyz"] and q["iss"] == [BASE]
    resp, raw = request(server, "POST", "/oauth/token", {"grant_type": "authorization_code", "code": q["code"][0],
                                                         "redirect_uri": CALLBACK, "client_id": client["client_id"],
                                                         "code_verifier": verifier}, FORM)
    assert resp.status == 200, raw
    return client, json.loads(raw)


def rpc(server, token, method, params=None, mid=1, session=None):
    msg = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}),
           **({"params": params} if params is not None else {})}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    return request(server, "POST", "/mcp", msg, headers)


def test_metadata_points_clients_at_sign_in(running):
    resp, raw = request(running, "GET", "/.well-known/oauth-protected-resource/mcp")
    assert json.loads(raw)["resource"] == BASE + "/mcp" and json.loads(raw)["authorization_servers"] == [BASE]
    resp, raw = request(running, "GET", "/.well-known/oauth-authorization-server")
    meta = json.loads(raw)
    assert meta["issuer"] == BASE and meta["registration_endpoint"] == BASE + "/oauth/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]


def test_mcp_needs_a_token(running):
    resp, _ = rpc(running, "nope", "tools/list")
    assert resp.status == 401
    assert resp.getheader("WWW-Authenticate") == (
        f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp", error="invalid_token"')
    resp, _ = request(running, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status == 401 and "error=" not in resp.getheader("WWW-Authenticate")


def test_a_connected_assistant_uses_the_tools(running):
    _, tokens = connect(running)
    token = tokens["access_token"]
    resp, raw = rpc(running, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                   "clientInfo": {"name": "example-assistant", "version": "1"}})
    assert resp.status == 200 and json.loads(raw)["result"]["serverInfo"]["name"] == "byobot"
    session = resp.getheader("Mcp-Session-Id")
    assert session is None
    resp, raw = rpc(running, token, "notifications/initialized", mid=None, session=session)
    assert resp.status == 202 and raw == b""
    resp, raw = rpc(running, token, "tools/list", session=session)
    assert "query" in {t["name"] for t in json.loads(raw)["result"]["tools"]}
    resp, raw = rpc(running, token, "tools/call", {"name": "log", "arguments": {
        "kind": "activity", "id": "x.plank", "payload": {"name": "Plank"}}}, session=session)
    assert json.loads(raw)["result"]["isError"] is False
    resp, raw = rpc(running, token, "tools/call", {"name": "query", "arguments": {
        "sql": "SELECT actor_kind, actor_id FROM events"}}, session=session)
    [row] = json.loads(raw)["result"]["structuredContent"]["rows"]
    assert row["actor_kind"] == "agent" and row["actor_id"].startswith("Example Assistant#")


def test_the_protocol_comes_from_the_header(running):
    _, tokens = connect(running)
    call = {"name": "query", "arguments": {"sql": "SELECT 1 AS one"}}
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json, text/event-stream"}
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}
    _, raw = request(running, "POST", "/mcp", msg, {**headers, "MCP-Protocol-Version": "2025-03-26"})
    assert "structuredContent" not in json.loads(raw)["result"]
    _, raw = request(running, "POST", "/mcp", msg, {**headers, "MCP-Protocol-Version": "2025-06-18"})
    assert json.loads(raw)["result"]["structuredContent"]["rows"] == [{"one": 1}]
    resp, _ = request(running, "DELETE", "/mcp", headers=headers)
    assert resp.status == 405


def test_without_a_session_writes_name_the_connected_app(running):
    _, tokens = connect(running)
    rpc(running, tokens["access_token"], "tools/call", {"name": "log", "arguments": {
        "kind": "activity", "id": "x.plank", "payload": {"name": "Plank"}}})
    resp, raw = rpc(running, tokens["access_token"], "tools/call", {"name": "query", "arguments": {
        "sql": "SELECT actor_id FROM events"}})
    [row] = json.loads(raw)["result"]["structuredContent"]["rows"]
    assert row["actor_id"].startswith("Example Assistant#")


def test_a_wrong_code_does_not_approve(running):
    resp, raw = request(running, "POST", "/oauth/register", {"redirect_uris": [CALLBACK],
                                                             "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    wrong = "000000" if running.pairing.code != "000000" else "111111"
    for _ in range(web.PAIRING_TRIES):
        resp, _ = request(running, "POST", "/oauth/authorize", {**params, "approval_code": wrong}, FORM)
        assert resp.status == 403 and resp.getheader("Location") is None
    resp, raw = request(running, "POST", "/oauth/authorize", {**params, "approval_code": running.pairing.code}, FORM)
    assert resp.status == 403 and b"Approval is closed" in raw


def test_an_unknown_return_address_is_not_redirected_to(running):
    resp, raw = request(running, "POST", "/oauth/register", {"redirect_uris": [CALLBACK],
                                                             "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    resp, _ = request(running, "GET", "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": "https://attacker.example/cb"}))
    assert resp.status == 400 and resp.getheader("Location") is None


def test_a_bad_request_is_redirected_with_an_error(running):
    resp, raw = request(running, "POST", "/oauth/register", {"redirect_uris": [CALLBACK],
                                                             "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    resp, _ = request(running, "GET", "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client["client_id"], "state": "s"}))
    assert resp.status == 302
    q = parse_qs(urlsplit(resp.getheader("Location")).query)
    assert q["error"] == ["invalid_request"] and q["state"] == ["s"]


def test_refresh_over_http_and_revoke(running):
    client, tokens = connect(running)
    resp, raw = request(running, "POST", "/oauth/token", {"grant_type": "refresh_token", "client_id": client["client_id"],
                                                          "refresh_token": tokens["refresh_token"]}, FORM)
    assert resp.status == 200
    fresh = json.loads(raw)
    assert rpc(running, fresh["access_token"], "ping")[0].status == 200
    Auth(running.auth.path).remove(client["client_id"])
    assert rpc(running, fresh["access_token"], "ping")[0].status == 401


def test_get_mcp_is_not_offered(running):
    resp, _ = request(running, "GET", "/mcp")
    assert resp.status == 405 and resp.getheader("Allow") == "POST"


def test_a_confidential_client_can_use_basic_auth(running):
    resp, raw = request(running, "POST", "/oauth/register", {"client_name": "Basic", "redirect_uris": [CALLBACK]})
    client = json.loads(raw)
    creds = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
    resp, raw = request(running, "POST", "/oauth/token", {"grant_type": "authorization_code", "code": "x"},
                        {**FORM, "Authorization": f"Basic {creds}"})
    assert resp.status == 400 and json.loads(raw)["error"] == "invalid_grant"
    bad = base64.b64encode(f"{client['client_id']}:wrong".encode()).decode()
    resp, raw = request(running, "POST", "/oauth/token", {"grant_type": "authorization_code", "code": "x"},
                        {**FORM, "Authorization": f"Basic {bad}"})
    assert resp.status == 401 and json.loads(raw)["error"] == "invalid_client"


# -- the command ----------------------------------------------------------------

def test_remote_needs_a_url_the_first_time(capsys):
    from byobot.cli import main

    assert main(["remote", "serve"]) == 1
    assert "--url" in json.loads(capsys.readouterr().err)["message"]
    assert main(["remote", "serve", "--url", "http://example.test"]) == 1


def test_device_list_and_remove_cover_assistants(capsys, auth):
    from byobot.cli import main

    client, _ = approved(auth)
    assert main(["device", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(d["id"], d["route"]) for d in listed["items"]] == [(client["client_id"], "sign_in")]
    assert main(["device", "remove", client["client_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["removed"]["name"] == "Example Assistant"
    assert auth.clients() == []


def test_remote_ports_differ_per_profile(monkeypatch, byobot_home):
    assert remote.choose_port() == remote.DEFAULT_PORT
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    assert remote.choose_port() > remote.DEFAULT_PORT


# -- API keys -------------------------------------------------------------------

def test_an_api_key_names_its_holder_until_revoked(auth):
    key = auth.create_key("example_assistant")
    assert key.startswith("byobot_") and key not in auth.path.read_text()
    assert auth.client_for_token(key)["client_name"] == "example_assistant"
    assert auth.client_for_token(key + "x") is None
    Auth(auth.path).remove(auth.assistants()[0]["id"])
    assert auth.client_for_token(key) is None


def test_api_key_names_are_checked(auth):
    with pytest.raises(remote.ValidationFailed):
        auth.create_key("Not OK")


# -- the plain JSON API -----------------------------------------------------------

def test_openapi_describes_every_tool(running):
    resp, raw = request(running, "GET", "/api/v1/openapi.json")
    spec = json.loads(raw)
    assert resp.status == 200 and spec["openapi"] == "3.1.0" and spec["servers"] == [{"url": BASE}]
    assert {p.rsplit("/", 1)[1] for p in spec["paths"]} == {t["name"] for t in remote.mcp.tools()}
    assert spec["paths"]["/api/v1/tools/query"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "required"] == ["sql"]


def test_the_api_needs_a_key(running):
    assert request(running, "GET", "/api/v1/tools")[0].status == 401
    assert request(running, "POST", "/api/v1/tools/query", {"sql": "SELECT 1"},
                   {"Authorization": "Bearer byobot_wrong"})[0].status == 401


@pytest.mark.parametrize("header", ["Authorization", "X-API-Key"])
def test_an_api_key_calls_tools(running, header):
    key = running.auth.create_key("example_assistant")
    auth = {header: f"Bearer {key}" if header == "Authorization" else key}
    resp, raw = request(running, "GET", "/api/v1/tools", headers=auth)
    assert resp.status == 200 and "log" in {t["name"] for t in json.loads(raw)["tools"]}
    resp, raw = request(running, "POST", "/api/v1/tools/log", {"kind": "activity", "id": "x.plank",
                                                               "payload": {"name": "Plank"}}, auth)
    assert resp.status == 200, raw
    resp, raw = request(running, "POST", "/api/v1/tools/query", {"sql": "SELECT actor_id FROM events"}, auth)
    [row] = json.loads(raw)["rows"]
    assert row["actor_id"].startswith("example_assistant#key:")
    resp, raw = request(running, "POST", "/api/v1/tools/query", {}, auth)
    assert resp.status == 400 and json.loads(raw)["error"] == "invalid"
    assert request(running, "POST", "/api/v1/tools/nope", {}, auth)[0].status == 404


def test_api_errors_carry_a_code_and_its_status(running):
    auth = {"X-API-Key": running.auth.create_key("example_assistant")}
    resp, raw = request(running, "POST", "/api/v1/tools/versions", {"kind": "activity", "id": "missing"}, auth)
    assert resp.status == 404 and json.loads(raw)["error"] == "not_found"
    resp, raw = request(running, "POST", "/api/v1/tools/nope", {}, auth)
    assert resp.status == 404 and json.loads(raw)["error"] == "not_found"
    assert request(running, "POST", "/api/tools/query", {"sql": "SELECT 1"}, auth)[0].status == 404
    spec = json.loads(request(running, "GET", "/api/v1/openapi.json")[1])
    assert spec["paths"]["/api/v1/tools/query"]["post"]["responses"]["404"]["content"]["application/json"][
        "schema"] == {"$ref": "#/components/schemas/Error"}


@pytest.mark.parametrize("tool, args", [
    ("validate", {"kind": "activity", "document": {"name": "Plank"}}),
    ("validate", {"kind": "observation", "document": {}}),
    ("versions", {"kind": "activity", "id": "missing"}),
    ("query", {"sql": "SELECT 1 AS one"}),
])
def test_the_api_answers_as_mcp_does(running, tool, args):
    key = running.auth.create_key("example_assistant")
    _, raw = request(running, "POST", f"/api/v1/tools/{tool}", args, {"X-API-Key": key})
    _, reply = rpc(running, key, "tools/call", {"name": tool, "arguments": args})
    assert json.loads(raw) == json.loads(json.loads(reply)["result"]["content"][0]["text"])


def test_an_api_read_that_runs_too_long_is_stopped(running, monkeypatch):
    monkeypatch.setattr(remote, "QUERY_TIMEOUT_S", 0)
    monkeypatch.setattr(query_module, "PROGRESS_INTERVAL", 1)
    resp, raw = request(running, "POST", "/api/v1/tools/status", {},
                        {"X-API-Key": running.auth.create_key("example_assistant")})
    assert resp.status == 503 and json.loads(raw)["error"] == "timeout"


def test_an_api_tool_that_breaks_keeps_its_text_to_the_log(running, monkeypatch):
    cmd = commands.REGISTRY["status"]

    def broken(ctx, **kw):
        raise RuntimeError("example secret detail")

    monkeypatch.setitem(commands.REGISTRY, "status", dataclasses.replace(cmd, handler=broken))
    resp, raw = request(running, "POST", "/api/v1/tools/status", {},
                        {"X-API-Key": running.auth.create_key("example_assistant")})
    assert resp.status == 500 and json.loads(raw)["error"] == "error" and b"secret" not in raw


def test_an_api_key_also_works_for_mcp(running):
    key = running.auth.create_key("example_assistant")
    resp, raw = rpc(running, key, "tools/list")
    assert resp.status == 200 and json.loads(raw)["result"]["tools"]


def test_a_read_only_key_reads_but_does_not_write(running):
    key = running.auth.create_key("example_assistant", remote.READ)
    auth = {"X-API-Key": key}
    names = {t["name"] for t in json.loads(request(running, "GET", "/api/v1/tools", headers=auth)[1])["tools"]}
    assert "query" in names and "log" not in names
    resp, raw = request(running, "POST", "/api/v1/tools/log", {"kind": "activity", "id": "x.plank",
                                                               "payload": {"name": "Plank"}}, auth)
    assert resp.status == 403 and json.loads(raw)["error"] == "forbidden"
    assert request(running, "POST", "/api/v1/tools/query", {"sql": "SELECT 1"}, auth)[0].status == 200
    assert "log" not in {t["name"] for t in json.loads(rpc(running, key, "tools/list")[1])["result"]["tools"]}


def test_approving_for_reading_only_grants_read(running):
    _, tokens = connect(running, read_only="1")
    assert tokens["scope"] == "read"
    resp, raw = rpc(running, tokens["access_token"], "tools/call", {"name": "log", "arguments": {
        "kind": "activity", "id": "x.plank", "payload": {"name": "Plank"}}})
    assert json.loads(raw)["result"]["isError"] is True


def test_remote_api_key_show_prints_a_working_key(capsys, auth):
    from byobot.cli import main

    assert main(["remote", "key", "example_assistant", "--show"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["openapi"].endswith("/api/v1/openapi.json")
    assert auth.client_for_token(out["key"])["client_name"] == "example_assistant"
    assert main(["remote", "key", "Bad Name", "--show"]) == 1


def test_remote_api_key_goes_on_the_clipboard_by_default(capsys, auth, monkeypatch):
    from byobot import remote
    from byobot.cli import main

    copied = []
    monkeypatch.setattr(remote.shutil, "which", lambda name: name == "pbcopy")
    monkeypatch.setattr(remote.subprocess, "run", lambda cmd, input, check: copied.append((cmd, input)))
    assert main(["remote", "key", "example_assistant"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "key" not in out and out["copied"]
    assert copied[0][0] == ["pbcopy"]
    assert auth.client_for_token(copied[0][1].decode())["client_name"] == "example_assistant"


def test_remote_api_key_fails_without_a_clipboard_unless_shown(capsys, auth, monkeypatch):
    from byobot import remote
    from byobot.cli import main

    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    assert main(["remote", "key", "example_assistant"]) == 1
    assert "--show" in capsys.readouterr().err
    assert not auth._load()["keys"] and main(["remote", "key", "--show"]) == 1


def test_removal_during_a_refresh_is_not_undone(auth):
    client = auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                                   "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    tokens = auth.exchange(auth.authenticate(client["client_id"], None), code, CALLBACK, verifier)

    # The refresh request has authenticated the client and is about to rotate its tokens when it is removed.
    authed = auth.authenticate(client["client_id"], None)
    original = auth._issue

    def issue_after_a_removal(c):
        Auth(auth.path).remove(client["client_id"])
        return original(c)

    auth._issue = issue_after_a_removal
    try:
        fresh = auth.refresh(authed, tokens["refresh_token"])
    except OAuthError:
        fresh = None
    auth._issue = original
    assert auth.clients() == [], "the removed client is connected again"
    assert fresh is None or auth.client_for_token(fresh["access_token"]) is None


def test_an_api_key_made_while_the_server_issues_tokens_is_kept(auth):
    client = auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                                   "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = auth.grant(client["client_id"], CALLBACK, challenge)

    authed = auth.authenticate(client["client_id"], None)
    cli = Auth(auth.path)  # `byobot remote key example_key`, from another process
    original = auth._load
    made = []

    def load_then_the_cli_writes():
        data = original()
        if not made:
            made.append(cli.create_key("example_key"))
        return data

    auth._load = load_then_the_cli_writes
    tokens = auth.exchange(authed, code, CALLBACK, verifier)
    auth._load = original

    assert auth.client_for_token(tokens["access_token"]) is not None
    assert auth.client_for_token(made[0]) is not None, "the key printed to the user doesn't work"


def test_a_write_that_first_waits_on_the_network_is_not_cut_off(running, byobot_home, monkeypatch):
    # import whoop spends most of their time on the network before they write. Here `log` stands in
    # for them: it waits longer than the deadline, then writes.
    monkeypatch.setattr(remote, "QUERY_TIMEOUT_S", 0.3)
    # A real import runs far more than the default 10,000 steps between checks; a one-record write here doesn't.
    monkeypatch.setattr(query_module, "PROGRESS_INTERVAL", 100)
    cmd = commands.REGISTRY["log"]

    def slow(ctx, **kw):
        time.sleep(0.6)
        return cmd.handler(ctx, **kw)

    monkeypatch.setitem(commands.REGISTRY, "log", dataclasses.replace(cmd, handler=slow))
    key = running.auth.create_key("example_assistant")
    resp, raw = request(running, "POST", "/api/v1/tools/log",
                        {"kind": "activity", "id": "x.plank", "payload": {"name": "Plank"}},
                        {"Authorization": f"Bearer {key}"})
    assert resp.status == 200, raw
    with Store(byobot_home / "byobot.db") as s:
        assert s.get("activity", "x.plank") is not None


def test_a_replayed_refresh_token_revokes_the_chain(auth):
    client = auth.register({"client_name": "Example Assistant", "redirect_uris": [CALLBACK],
                            "token_endpoint_auth_method": "none"})
    verifier, challenge = pkce()
    code = auth.grant(client["client_id"], CALLBACK, challenge)
    first = auth.exchange(auth.authenticate(client["client_id"], None), code, CALLBACK, verifier)
    c = auth.authenticate(client["client_id"], None)

    stolen = auth.refresh(c, first["refresh_token"])  # a copy of the token is used first, elsewhere
    with pytest.raises(OAuthError):
        auth.refresh(c, first["refresh_token"])  # the assistant's own refresh: a replay
    with pytest.raises(OAuthError):
        auth.refresh(c, stolen["refresh_token"])  # the replay should have revoked what replaced the token


def test_approval_stays_closed_when_an_api_key_is_connected(byobot_home):
    Auth(byobot_home / "remote" / "oauth.json", BASE + "/mcp").create_key("assistant")
    server = remote.make_server(BASE, "127.0.0.1", 0)
    try:
        assert server.pairing is None
    finally:
        server.server_close()


def test_the_approval_page_leads_with_where_the_app_goes(running):
    _, raw = request(running, "POST", "/oauth/register", {"client_name": "Example Assistant",
                                                          "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
              "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "xyz"}
    _, page = request(running, "GET", "/oauth/authorize?" + urlencode(params))
    assert b"calling itself" in page and b"<b>client.example</b>" in page


def test_an_unexpected_error_keeps_its_text_off_the_network(running, byobot_home, monkeypatch):
    key = Auth(byobot_home / "remote" / "oauth.json", BASE + "/mcp").create_key("assistant")

    def broken(*args, **kwargs):
        raise RuntimeError("/home/example-user/.byobot/byobot.db is locked")

    monkeypatch.setattr(commands, "run", broken)
    resp, raw = request(running, "POST", "/api/v1/tools/status", {}, {"Authorization": f"Bearer {key}"})
    assert resp.status == 500 and b"example-user" not in raw
    assert "internal error" in json.loads(raw)["message"]


def test_mcp_refuses_a_request_from_another_sites_page(running):
    _, tokens = connect(running)
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json, text/event-stream"}
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    resp, _ = request(running, "POST", "/mcp", ping, {**headers, "Origin": "https://attacker.example"})
    assert resp.status == 403
    resp, _ = request(running, "POST", "/mcp", ping, {**headers, "Origin": BASE})
    assert resp.status == 200
