"""The CLI, MCP and the JSON API come from one command registry, so agents see the same commands everywhere."""

import argparse
import re

from byobot import mcp, remote
from byobot.cli import build_parser
from byobot.commands import REGISTRY

# Setup and device admin, which belong to the person at the computer.
CLI_ONLY = {"init", "connect", "disconnect", "rebuild", "devices remove", "erase", "export", "restore", "ingest",
            "cloud setup", "cloud recover", "cloud join", "cloud remove", "cloud compact", "cloud rotate", "cloud off",
            "mcp", "serve", "remote serve", "remote key", "remote revoke"}


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
    assert all(c.cli for c in REGISTRY.values())


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
