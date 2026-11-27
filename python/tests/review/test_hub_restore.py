"""Review: `byobot serve` reads the store id once, at start. A restore into its (empty) store while it runs leaves it
naming the old id to phones, which then treat the restored logbook as another store."""

import threading

import pytest

from byobot import serve
from byobot.core import commands
from byobot.core.store import Store

from test_serve import ME, Client, _pair


@pytest.fixture
def hub(byobot_home):
    srv, info = serve.make_server("127.0.0.1", 0, pair=True)  # a new laptop: `byobot serve` made an empty store
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, info, Client(info["port"], info["fingerprint"])
    srv.shutdown()
    srv.server_close()


def test_a_restore_while_serving_is_what_phones_are_told(hub, byobot_home, tmp_path):
    _, info, client = hub
    with Store(tmp_path / "old-laptop.db") as old:
        commands.log(old, ME, [{"kind": "location", "id": "example-gym", "payload": {"name": "Example gym"}}])
        backup, original = list(old.export()), old.store_id
    with Store(byobot_home / "byobot.db") as s:  # `byobot restore backup.jsonl` in another terminal
        s.restore(backup)
        assert s.store_id == original

    status, hello = client.call("GET", "/v1/hello")
    assert status == 200
    paired = _pair(client, info)
    assert hello["server"]["store_id"] == original
    assert paired["server"]["store_id"] == original, (
        "the phone records another store, so it never sends again what the old laptop held after the backup")


def test_the_plan_names_the_restored_store(hub, byobot_home, tmp_path):
    _, info, client = hub
    paired = _pair(client, info)
    with Store(tmp_path / "old-laptop.db") as old:
        backup, original = list(old.export()), old.store_id
    with Store(byobot_home / "byobot.db") as s:
        s.restore(backup)
    client.token = paired["token"]
    status, plan = client.call("GET", "/v1/plan")
    assert status == 200 and plan["store_id"] == original
