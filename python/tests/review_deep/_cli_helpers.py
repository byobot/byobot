"""Helpers for the deep CLI / MCP / API review (prefix `cli`). Placeholders only: no real people or places.

Each entry point is driven with the same arguments and its answer is reduced to one of
("ok", result), ("error", error_code) or ("broken", what happened instead of an answer).
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading

from byobot import mcp, remote
from byobot.cli import main
from byobot.commands import REGISTRY, Command

BASE = "https://example-laptop.example.ts.net"


def _refuse(constant: str):
    raise ValueError(f"not JSON: {constant}")


def strict_loads(text: str):
    """JSON as RFC 8259 has it: NaN and Infinity are not JSON."""
    return json.loads(text, parse_constant=_refuse)


def shared() -> list[Command]:
    """Commands that the CLI, MCP and the JSON API all offer."""
    return [c for c in REGISTRY.values() if c.cli and c.mcp]


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


def via_cli(argv: list[str]) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    except BaseException as exc:  # noqa: BLE001 - a traceback is what we are looking for
        return ("broken", f"raised {type(exc).__name__}: {exc}")
    if code == 0:
        try:
            return ("ok", strict_loads(out.getvalue()))
        except ValueError as exc:
            return ("broken", f"exit 0 but stdout is not JSON: {exc}")
    try:
        body = strict_loads(err.getvalue())
    except ValueError:
        return ("broken", f"exit {code}, stderr not JSON: {err.getvalue().strip()[-160:]}")
    if code == 1 and isinstance(body, dict) and "error" in body:
        return ("error", body["error"])
    return ("broken", f"exit {code}: {body}")


def via_mcp(tool: str, args: dict) -> tuple:
    server = mcp.Server()
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "example-agent"}}})
    try:
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}})
    finally:
        server.close()
    if "error" in reply:
        return ("broken", f"JSON-RPC error {reply['error']}")
    result = reply["result"]
    body = json.loads(result["content"][0]["text"])
    if result["isError"]:
        return ("error", body.get("error") if isinstance(body, dict) else body)
    return ("ok", body)


class Api:
    """`byobot remote`'s JSON API, on a free port, with an API key."""

    def __init__(self, home):
        from byobot.core.store import Store

        Store(home / "byobot.db").close()
        self.server = remote.make_server(BASE, "127.0.0.1", 0, pair=False)
        self.key = self.server.auth.create_key("example_assistant")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def raw(self, path: str, body: bytes) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json",
                                                       "Authorization": f"Bearer {self.key}"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def call(self, tool: str, args: dict) -> tuple:
        status, data = self.raw(f"/api/tools/{tool}", json.dumps(args).encode())
        try:
            body = strict_loads(data)
        except ValueError as exc:
            return ("broken", f"HTTP {status}, body not JSON: {exc}")
        if status == 200:
            return ("ok", body)
        if status == 400 and isinstance(body, dict) and "error" in body:
            return ("error", body["error"])
        return ("broken", f"HTTP {status}: {body}")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
