"""The CLI, MCP and the JSON API come from one command registry, so agents see the same commands everywhere."""

import argparse
import http.client
import json
import re

import pytest

from byobot import mcp, remote
from byobot.cli import build_parser
from byobot.commands import REGISTRY, Command
from byobot.core.store import Store

from helpers import serving, strict_loads, via_cli, via_mcp

BASE = "https://example-laptop.example.ts.net"

# Setup and device admin, which belong to the person at the computer.
CLI_ONLY = {"init", "connect", "disconnect", "rebuild", "device remove", "erase", "export", "restore",
            "mcp", "serve", "remote serve", "remote key"}


def shared() -> list[Command]:
    """Commands that the CLI, MCP and the JSON API all offer."""
    return [c for c in REGISTRY.values() if c.mcp]


def filler(cmd: Command) -> dict:
    """Some value for every required argument, so a later check is the one that answers."""
    out = {}
    for p in cmd.params:
        if not p.required:
            continue
        if p.choices:
            out[p.name] = p.choices[0]
        elif p.type == "integer":
            out[p.name] = 1
        elif p.type == "json":
            out[p.name] = {}
        else:
            out[p.name] = "example-id"
    return out


def argv_for(cmd: Command, args: dict, extra: list[str] | None = None) -> list[str]:
    positional, flags = [], []
    for p in cmd.params:
        if p.name not in args:
            continue
        value = args[p.name]
        text = json.dumps(value) if p.type == "json" else str(value)
        if p.positional:
            positional.append(text)
        else:
            flags += [f"--{p.name.replace('_', '-')}", text]
    return [*cmd.name.split(" "), *positional, *flags, *(extra or [])]


class Api:
    """`byobot remote`'s JSON API, on a free port, with an API key."""

    def __init__(self, server):
        self.server = server
        self.key = server.auth.create_key("example_assistant")

    def raw(self, path: str, body: bytes) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json",
                                                       "Authorization": f"Bearer {self.key}"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def call(self, tool: str, args: dict) -> tuple:
        status, data = self.raw(f"/api/v1/tools/{tool}", json.dumps(args).encode())
        try:
            body = strict_loads(data)
        except ValueError as exc:
            return ("broken", f"HTTP {status}, body not JSON: {exc}")
        if status == 200:
            return ("ok", body)
        if 400 <= status < 500 and isinstance(body, dict) and "error" in body:
            return ("error", body["error"])
        return ("broken", f"HTTP {status}: {body}")


def cli_commands():
    """Each CLI command and its argument names, from the parser."""
    out = {}

    def walk(parser, prefix):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, f"{prefix} {name}".strip())
        if prefix:
            out[prefix] = {a.dest for a in parser._actions if a.dest not in ("help", "command", "subcommand")}

    walk(build_parser(), "")
    return out


def test_only_setup_commands_stay_off_mcp_and_the_api():
    assert {c.name for c in REGISTRY.values() if not c.mcp} == CLI_ONLY


def test_every_command_has_the_same_arguments_everywhere():
    cli = cli_commands()
    tools = {t["name"]: t for t in mcp.tools()}
    paths = remote.openapi("https://example.test")["paths"]
    for cmd in REGISTRY.values():
        params = {p.name for p in cmd.params}
        assert cli[cmd.name] == params, cmd.name
        if not cmd.mcp:
            assert cmd.tool not in tools and f"/api/v1/tools/{cmd.tool}" not in paths
            continue
        schema = tools[cmd.tool]["inputSchema"]
        assert set(schema["properties"]) - {"model"} == params, cmd.name
        assert paths[f"/api/v1/tools/{cmd.tool}"]["post"]["requestBody"]["content"]["application/json"]["schema"] == schema
    assert len(paths) == len(tools)


def test_tool_and_api_names_are_the_command_name_with_underscores():
    paths = remote.openapi("https://example.test")["paths"]
    for tool in mcp.tools():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", tool["name"]), tool["name"]
        assert paths[f"/api/v1/tools/{tool['name']}"]["post"]["operationId"] == tool["name"]
    assert {t["name"] for t in mcp.tools()} == {c.name.replace(" ", "_") for c in REGISTRY.values() if c.mcp}


@pytest.fixture
def api(byobot_home):
    Store(byobot_home / "byobot.db").close()
    with serving(remote.make_server(BASE, "127.0.0.1", 0, pair=False)) as server:
        yield Api(server)


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
        args["kind"] = "session"
    if cmd.name == "query":
        args["sql"] = "SELECT 1 AS x"
    if cmd.name == "validate":
        args.update({"document": {"name": "Example gym"}, "kind": "location"})
    answers = {"mcp": via_mcp(cmd.tool, args), "api": api.call(cmd.tool, args)}
    assert answers == {"mcp": ("error", "invalid"), "api": ("error", "invalid")}


@pytest.mark.parametrize("case", [c for c in INTEGER_CASES if c[0].read_only], ids=ids)
def test_cli_an_integer_too_big_for_the_store_is_a_json_error_everywhere(case, api):
    """CLI-3: `--limit 2**70` or `--weeks 2**70` is a traceback on the CLI, -32603 on MCP and HTTP 500 on the API."""
    cmd, param = case
    args = {**filler(cmd), param.name: 2 ** 70}
    if cmd.name == "list":
        args["kind"] = "session"
    if cmd.name == "query":
        args["sql"] = "SELECT 1 AS x"
    answers = {"mcp": via_mcp(cmd.tool, args), "api": api.call(cmd.tool, args), "cli": via_cli(argv_for(cmd, args))}
    assert answers == {k: ("error", "invalid") for k in answers}
