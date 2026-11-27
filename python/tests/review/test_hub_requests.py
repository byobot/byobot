"""Review: requests that anyone on the internet (remote, behind a tunnel) can send."""

import socket
import ssl
import threading

import pytest

from byobot import remote
from byobot.core.store import Store

BASE = "https://example-laptop.example.ts.net"


@pytest.fixture
def remote_server(byobot_home):
    Store(byobot_home / "byobot.db").close()
    server = remote.make_server(BASE, "127.0.0.1", 0, pair=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


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


def test_a_get_with_a_body_does_not_desync_the_next_request_on_the_connection(remote_server):
    # Behind a tunnel, one upstream connection carries requests from different callers, so leftover body bytes
    # would become the start of someone else's request.
    smuggled = b"GET /api/openapi.json HTTP/1.1\r\nHost: x\r\n\r\n"
    first = (b"GET /.well-known/oauth-authorization-server HTTP/1.1\r\nHost: x\r\nContent-Length: "
             + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled)
    second = b"GET /.well-known/oauth-protected-resource HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    sock = socket.create_connection(("127.0.0.1", remote_server.server_address[1]), timeout=5)
    reply = _exchange(sock, first + second)
    sock.close()
    answers = reply.count(b"HTTP/1.1 ")
    assert b'"openapi"' not in reply, "the body of the first request was answered as a request of its own"
    assert answers <= 2
