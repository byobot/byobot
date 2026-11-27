"""Hub deep review: malformed bodies get an HTTP answer, and pairing survives a failed or lost answer.

* Python's `json.loads` takes `NaN`, `Infinity` and `1e400` (which becomes inf). `/v1/samples` passes such a
  value to `ingest`, where `mac()`'s canonical JSON raises `ValueError`, which nothing catches: no answer.
* `json.loads` also raises a plain `ValueError` (not `JSONDecodeError`) for an integer longer than 4300 digits.
  `_body` doesn't catch it, so the request is dropped. On `byobot remote` this is the unauthenticated
  `/oauth/register`, reachable by anyone through the tunnel.
* `Pairing.check` spends the one-time code before the token is stored. If storing it fails (the write lock is
  held past the busy timeout) the phone gets no answer, and trying the same code again is refused as
  `pairing_closed`; the person must restart `byobot serve --pair`.
"""

import http.client
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _hub_helpers import hub, pair  # noqa: E402

from byobot import remote  # noqa: E402
from byobot.core.store import Store  # noqa: E402

BASE = "https://example-laptop.example.ts.net"


@pytest.fixture
def running(byobot_home):
    with hub() as h:
        yield h


def _sample(value: str) -> bytes:
    return ('{"samples": [{"source": "apple_health", "source_id": "example-sample-1", '
            '"type": "HKQuantityTypeIdentifierBodyMass", "start": "2026-11-01T08:00:00Z", '
            f'"end": "2026-11-01T08:00:00Z", "value": {value}, "unit": "kg"}}]}}').encode()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e400"])
def test_a_sample_with_a_number_json_cannot_hold_is_refused_with_an_answer(running, value):
    srv, info, client, _ = running
    pair(client, info)
    try:
        status, body = client.raw("POST", "/v1/samples", _sample(value))
    except ConnectionError as exc:
        pytest.fail(f"no HTTP answer for a sample valued {value}: {exc}")
    assert status == 400, body


def test_an_event_body_with_a_huge_integer_gets_an_answer(running):
    srv, info, client, _ = running
    pair(client, info)
    body = b'{"events": [], "kinds": {}, "x": ' + b"9" * 5000 + b"}"
    try:
        status, answer = client.raw("POST", "/v1/events", body)
    except ConnectionError as exc:
        pytest.fail(f"no HTTP answer: {exc}")
    assert status == 400, answer


@pytest.fixture
def remote_running(byobot_home):
    Store(byobot_home / "byobot.db").close()
    server = remote.make_server(BASE, "127.0.0.1", 0, pair=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_an_unauthenticated_registration_with_a_huge_integer_gets_an_answer(remote_running):
    body = (b'{"redirect_uris": ["https://client.example/cb"], "token_endpoint_auth_method": "none", "x": '
            + b"9" * 5000 + b"}")
    conn = http.client.HTTPConnection("127.0.0.1", remote_running.server_address[1], timeout=10)
    try:
        conn.request("POST", "/oauth/register", body=body, headers={"Content-Type": "application/json"})
        try:
            resp = conn.getresponse()
        except (http.client.RemoteDisconnected, ConnectionResetError) as exc:
            pytest.fail(f"a stranger's registration got no HTTP answer: {exc!r}")
        assert resp.status == 400
    finally:
        conn.close()


def test_a_pairing_whose_token_could_not_be_stored_can_be_tried_again(running, monkeypatch):
    srv, info, client, _ = running
    real = Store.pair
    calls = []

    def flaky(self, *args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")  # the CLI held the write lock past 5 s
        return real(self, *args)

    monkeypatch.setattr(Store, "pair", flaky)
    request = {"code": info["code"], "device_id": "0193a0b0-0000-7000-8000-00000000ab32", "name": "Example iPhone"}
    try:
        client.call("POST", "/v1/pair", request)
    except ConnectionError:
        pass  # no answer today; a 503 would be right too
    status, body = client.call("POST", "/v1/pair", request)
    assert status == 200, body
