"""Deep review (cli): one command registry, so the CLI, MCP and the JSON API must answer the same input the same way.

These walk the registry and drive every command that all three offer with the same arguments.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import Api, filler, shared, via_mcp  # noqa: E402


@pytest.fixture
def api(byobot_home):
    server = Api(byobot_home)
    yield server
    server.close()


READ_CASES = [c for c in shared() if c.read_only]


def ids(case):
    return case.name if not isinstance(case, tuple) else f"{case[0].name}:{case[1].name}"


@pytest.mark.parametrize("cmd", READ_CASES, ids=ids)
def test_cli_an_argument_the_command_does_not_take_is_refused_on_mcp_and_the_api(cmd, api):
    """CLI-2: the tool schema says additionalProperties false and the CLI refuses an unknown flag, but MCP and the
    API drop an unknown argument and answer as if it weren't there."""
    args = {**filler(cmd), "not_a_param": "x"}
    if cmd.name == "list":
        args["kind"] = "sessions"
    if cmd.name == "query":
        args["sql"] = "SELECT 1 AS x"
    if cmd.name == "validate":
        args.update({"document": {"name": "Example gym"}, "as": "location"})
    answers = {"mcp": via_mcp(cmd.tool, args), "api": api.call(cmd.tool, args)}
    assert answers == {"mcp": ("error", "invalid"), "api": ("error", "invalid")}
