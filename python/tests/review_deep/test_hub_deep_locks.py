"""Hub deep review: a long write elsewhere (an import, `byobot rebuild`, the Drive poller taking in a big
segment) must not make the hub drop the phone's requests without an answer.

Every request opens a `Store`, whose `_migrate` always runs `BEGIN IMMEDIATE` and writes meta, and `_auth` writes
`last_seen_at`. So even a plan read needs SQLite's write lock. When another connection holds it past the 5 s
busy timeout, `OperationalError: database is locked` escapes the handler and the connection is closed with no
HTTP answer. The app pulls the plan first, so it never pushes, and tells the person their laptop can't be
reached. In WAL mode a reader doesn't need to wait for a writer at all."""

import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _hub_helpers import hub, location, pair  # noqa: E402

HOLD_S = 6.5  # longer than the store's 5 s busy timeout


@pytest.fixture
def running(byobot_home):
    with hub() as h:
        yield h


def _hold_write_lock(db: Path, ready: threading.Event, seconds: float) -> None:
    conn = sqlite3.connect(db, isolation_level=None, timeout=10)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO meta VALUES ('example_long_write', '1') ON CONFLICT (key) DO NOTHING")
    ready.set()
    time.sleep(seconds)
    conn.execute("COMMIT")
    conn.close()


def _while_another_process_writes(db: Path, request):
    ready = threading.Event()
    writer = threading.Thread(target=_hold_write_lock, args=(db, ready, HOLD_S), daemon=True)
    writer.start()
    ready.wait(5)
    try:
        return request()
    finally:
        writer.join(15)


def test_a_plan_read_while_another_process_writes_is_answered(running, byobot_home):
    srv, info, client, _ = running
    pair(client, info)
    try:
        status, body = _while_another_process_writes(byobot_home / "byobot.db",
                                                     lambda: client.call("GET", "/v1/plan", timeout=20))
    except ConnectionError as exc:
        pytest.fail(f"the plan read got no HTTP answer while another process wrote: {exc}")
    assert status == 200, body


def test_a_push_while_another_process_writes_gets_an_answer(running, byobot_home):
    srv, info, client, _ = running
    pair(client, info)
    events = [location("example_park", "Example park")]
    try:
        status, body = _while_another_process_writes(
            byobot_home / "byobot.db",
            lambda: client.call("POST", "/v1/events", {"events": events, "kind_versions": {}}, timeout=20))
    except ConnectionError as exc:
        pytest.fail(f"the push got no HTTP answer while another process wrote: {exc}")
    assert status in (200, 503), body
