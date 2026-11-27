"""Review: Handler.setup() runs once per connection, not per request. So the whole-request deadline and the
"body consumed" / "100 Continue sent" flags span every request on a keep-alive connection, which URLSession reuses."""

import json
import socket
import ssl
import threading
import time

import pytest

from byobot import serve

from test_serve import Client, _pair


@pytest.fixture
def hub(byobot_home, monkeypatch):
    monkeypatch.setattr(serve, "REQUEST_DEADLINE_S", 2)  # 30 s in production; scaled down to keep the test short
    srv, info = serve.make_server("127.0.0.1", 0, pair=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, info, Client(info["port"], info["fingerprint"])
    srv.shutdown()
    srv.server_close()


def _connect(port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5))


def _response(sock) -> tuple[int, dict, bytes]:
    """One HTTP/1.1 response read off a kept-alive connection."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError(f"closed after {data!r}")
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    headers = {k.lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:])}
    length = int(headers.get("content-length", 0))
    while len(rest) < length:
        rest += sock.recv(4096)
    return int(lines[0].split()[1]), headers, rest[:length]


def _post(path, token, body: bytes) -> bytes:
    return (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body


def test_each_request_on_a_kept_alive_connection_gets_its_own_deadline(hub):
    _, info, client = hub
    token = _pair(client, info)["token"]
    sock = _connect(info["port"])
    statuses = []
    # A first sync sends a year of Apple Health readings, a thousand at a time, one request after another.
    for _ in range(30):
        sock.sendall(_post("/v1/samples", token, json.dumps({"samples": []}).encode()))
        try:
            status, _, _ = _response(sock)
        except ConnectionError:
            status = "closed"
        statuses.append(status)
        if status != 200:
            break
        time.sleep(0.1)  # each request is quick, and well inside the idle timeout
    sock.close()
    assert statuses == [200] * 30, statuses


def test_a_refusal_after_an_earlier_post_on_the_connection_does_not_desync_it(hub):
    _, info, client = hub
    token = _pair(client, info)["token"]
    sock = _connect(info["port"])
    sock.sendall(_post("/v1/samples", token, json.dumps({"samples": []}).encode()))
    assert _response(sock)[0] == 200
    # Refused before its body is read: the body must be dropped or the connection closed, never parsed.
    inner = b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n\r\n"
    sock.sendall(_post("/v1/events", "not-a-token", inner))
    status, headers, _ = _response(sock)
    assert status == 401
    assert headers.get("connection", "").lower() == "close", "the unread body stays on a kept-alive connection"
    sock.settimeout(2)
    try:
        extra = _response(sock)
    except (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError, OSError):
        extra = None
    assert extra is None, f"the body was answered as a request of its own: {extra}"
