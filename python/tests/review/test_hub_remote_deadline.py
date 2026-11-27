"""Review: remote puts every tool, writes included, under the read deadline, timed from the start of the request."""

import dataclasses
import json
import threading
import time

import pytest

from byobot import commands, remote, serve
from byobot.core.store import Store

from test_remote import BASE, request


@pytest.fixture
def running(byobot_home):
    Store(byobot_home / "byobot.db").close()
    server = remote.make_server(BASE, "127.0.0.1", 0, pair=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_a_write_that_first_waits_on_the_network_is_not_cut_off(running, byobot_home, monkeypatch):
    # import-whoop and cloud-sync spend most of their time on the network before they write. Here `log` stands in
    # for them: it waits longer than the deadline, then writes.
    monkeypatch.setattr(remote, "QUERY_TIMEOUT_S", 0.3)
    # A real import runs far more than the default 10,000 steps between checks; a one-record write here doesn't.
    monkeypatch.setattr(serve, "QUERY_PROGRESS_INTERVAL", 100)
    cmd = commands.REGISTRY["log"]

    def slow(ctx, **kw):
        time.sleep(0.6)
        return cmd.handler(ctx, **kw)

    monkeypatch.setitem(commands.REGISTRY, "log", dataclasses.replace(cmd, handler=slow))
    key = running.auth.create_key("example_assistant")
    resp, raw = request(running, "POST", "/api/v1/tools/log",
                        {"kind": "activity", "id": "plank", "payload": {"name": "Plank"}},
                        {"Authorization": f"Bearer {key}"})
    assert resp.status == 200, raw
    with Store(byobot_home / "byobot.db") as s:
        assert s.get("activity", "plank") is not None
