"""Hub deep review: every event a phone pushes is kept or reported refused, exactly once, even when a push fails
part way.

`accept_events` (core/relay.py) commits each event in its own transaction and records the refusals only after
the loop. A failure in the middle of a batch (SQLite's "database is locked" after its 5 s busy timeout while the
CLI, an import or the Drive poller holds the write lock, or the process being killed) leaves the events before it
committed, so `received` (the latest clock reading held from the phone) moves past them, while the refusals among
them are never recorded. The phone gets no answer. When it then leaves this laptop (pairs with another, or
forgets it), `Logbook.settle(with:)` treats every waiting event at or below `received` that isn't listed in
`rejected` as taken, so the refused event is dropped from `pending` without ever being set aside: it is neither
kept nor reported."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _hub_helpers import PHONE, event, hub, location, no_answer, pair  # noqa: E402

from byobot.core.store import Store  # noqa: E402


@pytest.fixture
def running(byobot_home):
    with hub() as h:
        yield h


def _accounted(window: dict, pushed: list[dict], held: set[str]) -> list[str]:
    """Ids the phone's settle would drop although the hub neither holds nor lists them as refused."""
    marker = window["received"].get(PHONE)
    refused = {r["id"] for r in window["rejected"]}
    return [e["id"] for e in pushed if marker and e["hlc"] <= marker and e["id"] not in held
            and e["id"] not in refused]


def test_a_refusal_before_a_failure_mid_push_is_still_reported(running, byobot_home, monkeypatch):
    srv, info, client, _ = running
    pair(client, info)
    refused = location("example_gym", "Example gym")
    refused["actor"] = {"kind": "agent", "id": "example-coach"}  # refused: a phone pushes only as its person
    kept = location("example_park", "Example park")
    cut = location("example_pool", "Example pool")

    real = Store.accept

    def accept(self, e):
        if isinstance(e, dict) and e.get("id") == cut["id"]:
            # What SQLite raises when another process held the write lock past the 5 s busy timeout.
            raise sqlite3.OperationalError("database is locked")
        return real(self, e)

    monkeypatch.setattr(Store, "accept", accept)
    pushed = [refused, kept, cut]
    no_answer(lambda: client.call("POST", "/v1/events", {"events": pushed, "kinds": {}}))
    monkeypatch.setattr(Store, "accept", real)

    status, window = client.call("GET", "/v1/plan")
    assert status == 200, window
    with Store(byobot_home / "byobot.db") as s:
        held = {e["id"] for e in pushed if s.event(e["id"])}
    assert kept["id"] in held  # committed before the failure, so `received` already names it
    assert _accounted(window, pushed, held) == [], (
        "the refused event is at or below `received` but not in `rejected`, so the phone's settle drops it")


def test_a_push_that_fails_part_way_still_answers(running, monkeypatch):
    """The phone should learn the outcome of the events that went in, not get a dropped connection."""
    srv, info, client, _ = running
    pair(client, info)
    first, second = location("example_park", "Example park"), location("example_pool", "Example pool")
    real = Store.accept

    def accept(self, e):
        if isinstance(e, dict) and e.get("id") == second["id"]:
            raise sqlite3.OperationalError("database is locked")
        return real(self, e)

    monkeypatch.setattr(Store, "accept", accept)
    try:
        status, body = client.call("POST", "/v1/events", {"events": [first, second], "kinds": {}})
    except ConnectionError as exc:
        pytest.fail(f"the push got no HTTP answer: {exc}")
    assert status in (200, 503), body
