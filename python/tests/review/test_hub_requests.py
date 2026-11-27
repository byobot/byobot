"""Review: requests that anyone on the network (the hub) or on the internet (remote, behind a tunnel) can send."""

import hashlib
import http.client
import json
import socket
import ssl
import threading

import pytest

from byobot import remote, serve
from byobot.core.store import Store

from test_serve import Client, _pair

BASE = "https://example-laptop.example.ts.net"


@pytest.fixture
def hub(byobot_home):
    logs = []
    srv, info = serve.make_server("127.0.0.1", 0, pair=True, log=logs.append)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, info, Client(info["port"], info["fingerprint"]), logs
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def remote_server(byobot_home):
    Store(byobot_home / "byobot.db").close()
    server = remote.make_server(BASE, "127.0.0.1", 0, pair=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _tls(port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5))


def _exchange(sock, raw: bytes) -> bytes:
    sock.sendall(raw)
    out = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out += chunk
    except (TimeoutError, socket.timeout, ConnectionError, ssl.SSLError):
        pass
    return out


def test_a_pairing_code_in_other_digits_gets_an_answer_and_spends_a_try(hub):
    srv, info, client, _ = hub
    # "١٢٣٤٥٦" is 123456 in Arabic-Indic digits: not ASCII, so secrets.compare_digest raises TypeError.
    body = json.dumps({"code": "١٢٣٤٥٦", "device_id": "example-phone",
                       "name": "Example iPhone"}).encode()
    sock = _tls(info["port"])
    reply = _exchange(sock, b"POST /v1/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                            b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sock.close()
    assert reply.startswith(b"HTTP/1.1 403"), f"no answer at all: {reply[:80]!r}"
    assert srv.pairing.tries_left == serve.PAIRING_TRIES - 1


def test_an_approval_code_in_other_digits_gets_a_page(remote_server):
    from test_remote import CALLBACK, FORM, pkce, request

    resp, raw = request(remote_server, "POST", "/oauth/register", {"client_name": "Example Assistant",
                                                                   "redirect_uris": [CALLBACK],
                                                                   "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz"}
    try:
        resp, _ = request(remote_server, "POST", "/oauth/authorize", {**params, "approval_code": "¹²"}, FORM)
        status = resp.status
    except (http.client.HTTPException, ConnectionError):
        status = None
    assert status == 403, "the approval page dropped the connection instead of answering"


def test_a_pull_cursor_in_other_digits_is_a_400(hub):
    _, info, client, _ = hub
    _pair(client, info)
    try:
        status, _ = client.call("GET", "/v1/events?after=%C2%B2")  # "²": str.isdigit() is True, int() fails
    except (http.client.HTTPException, ConnectionError):
        status = None
    assert status == 400


def test_the_log_never_carries_terminal_control_codes_from_a_stranger(hub):
    _, info, _, logs = hub
    sock = _tls(info["port"])
    # ESC ] 0 ; ... BEL sets the terminal's title; other sequences can rewrite what `byobot serve` showed.
    _exchange(sock, b"GET /v1/\x1b]0;pwned\x07\x1b[2J HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    sock.close()
    assert logs, "nothing was logged"
    assert not any("\x1b" in line or "\x07" in line for line in logs), logs


def test_registration_with_a_malformed_grant_types_gets_an_oauth_error(remote_server):
    from test_remote import CALLBACK, request

    try:
        resp, raw = request(remote_server, "POST", "/oauth/register",
                            {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "grant_types": 5})
        status = resp.status
    except (http.client.HTTPException, ConnectionError):
        status = None
    assert status == 400, "an unauthenticated registration dropped the connection instead of answering"


def test_a_client_that_never_sends_redirect_uri_can_finish_sign_in(remote_server):
    """RFC 6749 4.1.3 (kept by OAuth 2.1): redirect_uri is required at the token endpoint only if it was in the
    authorization request. byobot lets a client with one registered address leave it out of the request, then
    insists on it at the token endpoint."""
    from urllib.parse import parse_qs, urlencode, urlsplit

    from test_remote import CALLBACK, FORM, pkce, request

    resp, raw = request(remote_server, "POST", "/oauth/register", {"client_name": "Example Assistant",
                                                                   "redirect_uris": [CALLBACK],
                                                                   "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "xyz"}  # no redirect_uri
    resp, _ = request(remote_server, "GET", "/oauth/authorize?" + urlencode(params))
    assert resp.status == 200
    resp, _ = request(remote_server, "POST", "/oauth/authorize",
                      {**params, "approval_code": remote_server.pairing.code}, FORM)
    assert resp.status == 302
    code = parse_qs(urlsplit(resp.getheader("Location")).query)["code"][0]
    resp, raw = request(remote_server, "POST", "/oauth/token", {"grant_type": "authorization_code", "code": code,
                                                               "client_id": client["client_id"],
                                                               "code_verifier": verifier}, FORM)
    assert resp.status == 200, raw
