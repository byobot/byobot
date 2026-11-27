"""Hub deep review: the plan a phone pulls, and the pushes around it.

* `plan_window` (core/relay.py) runs about fifteen queries in autocommit mode, so a write that lands between
  them is half seen. An erase that commits after the entries are read and before `erased` is read gives a window
  that lists the record as live (with its full payload) and as erased at once. The phone drops its own events
  for it but keeps showing the laptop's copy of what was erased until the next pull, and may edit it, writing
  the erased content back as a new put.
* After an erase, `compact_pending` stays set until a VACUUM and a full WAL checkpoint both succeed. While any
  other connection has a read open, the checkpoint can't finish, and `accept_many` runs `_compact()` (a VACUUM of
  the whole file) after every single event. A phone's batch of 500 events then rewrites the whole database 500
  times, so the push runs far past the app's 8 s request timeout.
"""

import sqlite3

from byobot.core import commands, relay
from byobot.core.store import Store

PHONE = "0193a0b0-0000-7000-8000-00000000ab33"
ME = {"kind": "human", "id": "me"}


def test_a_plan_is_one_snapshot_even_when_an_erase_lands_while_it_is_read(store, byobot_home, monkeypatch):
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    real = relay._device_state

    def erase_meanwhile(s, device_id):
        with Store(byobot_home / "byobot.db") as other:  # the CLI, on its own connection
            commands.erase(other, ME, "location", "example_gym")
        return real(s, device_id)

    monkeypatch.setattr(relay, "_device_state", erase_meanwhile)
    window = relay.plan_window(store, "2026-11-01", "2026-11-14", PHONE)
    live = {e["id"] for e in window["locations"]}
    erased = {e["id"] for e in window["erased"] if e["kind"] == "location"}
    assert not live & erased, "the window shows example_gym as live, with its payload, and as erased"


def test_a_push_after_an_erase_compacts_the_file_at_most_once(store, byobot_home):
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    reader = sqlite3.connect(byobot_home / "byobot.db", isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM events").fetchone()  # another process mid-read, e.g. a remote tool call
    commands.erase(store, ME, "location", "example_gym")
    assert store.compact_pending()

    vacuums = []
    store.conn.set_trace_callback(lambda sql: vacuums.append(sql) if sql.strip().upper() == "VACUUM" else None)
    from byobot.core import hlc
    from byobot.util import uuid7
    events = []
    for i in range(20):
        events.append({"id": uuid7(), "schema_version": 1, "record": {"kind": "location", "id": f"example_place_{i}"},
                       "op": "put", "kind_version": 1, "hlc": hlc.format((1795700000000 + i * 1000, 0)),
                       "ts": "2026-11-23T08:00:00-05:00", "device_id": PHONE, "actor": ME, "prev": [],
                       "payload": {"name": f"Example place {i}"}})
    results = relay.accept_events(store, PHONE, events)
    reader.execute("COMMIT")
    reader.close()
    assert all(r["status"] == "accepted" for r in results)
    assert len(vacuums) <= 1, f"one push of {len(events)} events ran VACUUM {len(vacuums)} times"
