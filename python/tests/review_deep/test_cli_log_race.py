"""Deep review (cli): `log` refuses an id already in use, "never overwritten", even when two writers race."""

import pytest

from byobot.core import commands as core
from byobot.core.store import Store
from byobot.util import Conflict

ME = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "example-agent"}


def test_cli_two_logs_of_the_same_new_id_never_overwrite_each_other(byobot_home):
    """CLI-10: core.log checks that the id is free before its write transaction. When another process creates the
    same id in between (an agent over MCP and the person at the CLI both adding `example-gym`), the second write
    names the first as its prev and goes through as an edit: the first writer's record is silently replaced."""
    path = byobot_home / "byobot.db"
    mine, theirs = Store(path), Store(path)
    real = mine.write

    def write(actor, changes):
        core.log(theirs, AGENT, [{"kind": "location", "id": "example_gym", "payload": {"name": "Agent's gym"}}])
        return real(actor, changes)

    mine.write = write
    try:
        with pytest.raises(Conflict):
            core.log(mine, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "My gym"}}])
        assert mine.get("location", "example_gym")["payload"]["name"] == "Agent's gym"
    finally:
        mine.close()
        theirs.close()
