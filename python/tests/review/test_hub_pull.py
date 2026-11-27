"""Review: GET /v1/events computes `next` in a second query, so an event committed between the two is skipped."""

import threading

import pytest

from byobot import serve
from byobot.core.store import Store

from test_serve import Client, _event, _pair, seed  # noqa: F401


@pytest.fixture
def hub(byobot_home):
    srv, info = serve.make_server("127.0.0.1", 0, pair=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, info, Client(info["port"], info["fingerprint"])
    srv.shutdown()
    srv.server_close()


def test_an_event_landing_between_the_two_pull_queries_is_not_skipped(hub, byobot_home, monkeypatch):
    _, info, client = hub
    with Store(byobot_home / "byobot.db") as s:
        seed(s)
    _pair(client, info)
    status, first = client.call("GET", "/v1/events")
    assert status == 200 and first["more"] is False
    cursor = first["next"]

    # Another writer (the CLI, an agent over MCP, the Drive poller, another phone's push) commits an event right
    # after the hub has read the page but before it asks for the page's last position.
    late = _event("activity", "x.plank", {"name": "Plank"}, device_id="0193a0b0-0000-7000-8000-00000000ab02")
    original = Store.events
    armed = [True]

    def events_then_a_concurrent_write(self, since=0, limit=-1):
        out = original(self, since, limit)
        if armed[0] and since == cursor:
            armed[0] = False
            with Store(byobot_home / "byobot.db") as other:
                assert other.accept(late)[0] == "accepted"
        return out

    monkeypatch.setattr(Store, "events", events_then_a_concurrent_write)
    status, page = client.call("GET", f"/v1/events?after={cursor}")
    assert status == 200 and page["events"] == []
    status, after = client.call("GET", f"/v1/events?after={page['next']}")
    assert status == 200
    pulled = [e["id"] for e in page["events"] + after["events"]]
    assert late["id"] in pulled, "the cursor moved past an event the puller never received"
