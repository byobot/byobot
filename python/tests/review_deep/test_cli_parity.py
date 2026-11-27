"""Deep review (cli): one command registry, so the CLI, MCP and the JSON API must answer the same input the same way.

These walk the registry and drive every command that all three offer with the same arguments.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import Api, argv_for, filler, shared, via_cli, via_mcp  # noqa: E402


@pytest.fixture
def api(byobot_home):
    server = Api(byobot_home)
    yield server
    server.close()


CHOICE_CASES = [(c, p) for c in shared() for p in c.params if p.choices]
REQUIRED_CASES = [c for c in shared() if any(p.required for p in c.params)]
INTEGER_CASES = [(c, p) for c in shared() for p in c.params if p.type == "integer"]
READ_CASES = [c for c in shared() if c.read_only]


def ids(case):
    return case.name if not isinstance(case, tuple) else f"{case[0].name}:{case[1].name}"


@pytest.mark.parametrize("case", CHOICE_CASES, ids=ids)
def test_cli_a_value_outside_the_choices_is_the_same_json_error_everywhere(case, api):
    """CLI-1: argparse answers a bad choice with usage text and exit 2; MCP and the API answer {"error": "invalid"}."""
    cmd, param = case
    args = {**filler(cmd), param.name: "not-a-choice"}
    answers = {"mcp": via_mcp(cmd.tool, args), "api": api.call(cmd.tool, args), "cli": via_cli(argv_for(cmd, args))}
    assert answers == {k: ("error", "invalid") for k in answers}


@pytest.mark.parametrize("cmd", REQUIRED_CASES, ids=ids)
def test_cli_a_missing_argument_is_the_same_json_error_everywhere(cmd, api):
    """CLI-1: a missing required argument is usage text and exit 2 on the CLI only."""
    answers = {"mcp": via_mcp(cmd.tool, {}), "api": api.call(cmd.tool, {}), "cli": via_cli(argv_for(cmd, {}))}
    assert answers == {k: ("error", "invalid") for k in answers}


@pytest.mark.parametrize("case", INTEGER_CASES, ids=ids)
def test_cli_a_number_that_is_not_an_integer_is_the_same_json_error_everywhere(case, api):
    """CLI-1: `--limit 1.5` is usage text and exit 2 on the CLI; elsewhere "limit must be an integer"."""
    cmd, param = case
    args = {**filler(cmd), param.name: 1.5}
    answers = {"mcp": via_mcp(cmd.tool, args), "api": api.call(cmd.tool, args), "cli": via_cli(argv_for(cmd, args))}
    assert answers == {k: ("error", "invalid") for k in answers}


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


@pytest.mark.parametrize("case", [c for c in INTEGER_CASES if c[0].read_only], ids=ids)
def test_cli_an_integer_too_big_for_the_store_is_a_json_error_everywhere(case, api):
    """CLI-3: `--limit 2**70` or `--weeks 2**70` is a traceback on the CLI, -32603 on MCP and HTTP 500 on the API."""
    cmd, param = case
    args = {**filler(cmd), param.name: 2 ** 70}
    if cmd.name == "list":
        args["kind"] = "sessions"
    if cmd.name == "query":
        args["sql"] = "SELECT 1 AS x"
    answers = {"mcp": via_mcp(cmd.tool, args), "api": api.call(cmd.tool, args), "cli": via_cli(argv_for(cmd, args))}
    assert answers == {k: ("error", "invalid") for k in answers}
