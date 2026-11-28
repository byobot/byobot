"""What several test modules share."""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from byobot.core.store import Store

REPO = next(p for p in Path(__file__).resolve().parents if (p / "schema" / "v1").is_dir())
FIXTURES = REPO / "schema" / "fixtures" / "v1"

ME = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "example-agent"}
PHONE = "0193a0b0-0000-7000-8000-00000000ab01"
T0 = datetime(2026, 3, 2, 10, tzinfo=timezone(timedelta(hours=-5)))
HOLD_S = 6.5  # longer than the store's 5 s busy timeout, as a big ingest, restore or rebuild is


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class Wall:
    """A clock that moves one second each time it is read."""

    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


def open_store(path, device_id="laptop", wall=None, **kw) -> Store:
    return Store(path, device_id=device_id, wall=wall or Wall(), **kw)


def peer(store: Store, path, device_id="laptop", **kw) -> Store:
    """A second, empty store of the same logbook."""
    other = open_store(path, device_id, **kw)
    other.adopt(store.store_id, store.store_key.hex())
    return other


def effort(reps=5):
    return {"activity_id": "row", "time": {"start": "2026-03-02T10:00:00-05:00"},
            "metrics": {"reps": {"value": reps}}}


def reps(store, rid):
    return store.get("effort", rid)["payload"]["metrics"]["reps"]["value"]


def sync(*stores):
    for a in stores:
        for b in stores:
            if a is not b:
                b.accept_many(a.events())


def whoop_workout(wid: str, start: str, end: str, updated="2026-03-02T16:00:00.000Z", strain=10.0, hr=140,
                  offset="-05:00") -> dict:
    return {"id": wid, "start": start, "end": end, "timezone_offset": offset, "updated_at": updated,
            "sport_name": "running", "score_state": "SCORED",
            "score": {"strain": strain, "average_heart_rate": hr, "max_heart_rate": hr + 30}}


@contextmanager
def serving(server):
    """Serve on a thread until the block ends."""
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _hold_write_lock(db: Path, ready: threading.Event) -> None:
    conn = sqlite3.connect(db, isolation_level=None, timeout=10)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO meta VALUES ('example_long_write', '1') ON CONFLICT (key) DO NOTHING")
    ready.set()
    time.sleep(HOLD_S)
    conn.execute("COMMIT")
    conn.close()


def while_another_process_writes(db: Path, request):
    """Run `request` while another connection holds the store's write lock."""
    ready = threading.Event()
    writer = threading.Thread(target=_hold_write_lock, args=(db, ready), daemon=True)
    writer.start()
    ready.wait(5)
    try:
        return request()
    finally:
        writer.join(15)
