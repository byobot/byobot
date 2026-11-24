"""``byobot mcp``: the command registry served as an MCP server over stdio.

A small, dependency-free implementation of the parts of the Model Context
Protocol this server needs: ``initialize``, ``ping``, ``tools/list``,
``tools/call``, ``prompts/list`` and ``prompts/get``, as newline-delimited JSON-RPC 2.0 on stdin/stdout.
Logs go to stderr. Writes are attributed to the connected agent: its
``clientInfo.name`` becomes the actor id, and a tool call may name its model.
"""

from __future__ import annotations

import json
import os
import sys
from typing import IO, Any

from byobot import __version__, prompts
from byobot.commands import REGISTRY, Command, Context, run
from byobot.util import ByobotError, db_path

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]

INSTRUCTIONS = (
    "byobot is the user's personal health store. Read with `list`, `query` (read-only SQL; see `schema` with "
    "name 'sql' for tables; plan_vs_actual and strength_sets are ready-made views) and `schema`. Write with `log` "
    "(activities, locations, plans, planned sessions, performed sessions and efforts; metrics are an open map such as "
    "{\"reps\": {\"value\": 5}, \"load\": {\"value\": 100, \"unit\": \"kg\"}}, and your own metrics start with x.), "
    "`ingest` (telemetry) and `undo`. Every write is recorded with you as its author and can be undone. "
    "Loads compare only within one location (gym): compare a set with earlier sets at its location_id. "
    "Check a document with `validate` before writing if you are unsure of its shape."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _input_schema(cmd: Command) -> dict:
    props: dict[str, Any] = {}
    required = []
    for p in cmd.params:
        if p.type == "json":
            prop: dict[str, Any] = dict(p.schema or {})
        else:
            prop = {"type": p.type}
        prop["description"] = p.help
        if p.choices:
            prop["enum"] = list(p.choices)
        if p.default is not None:
            prop["default"] = p.default
        props[p.name] = prop
        if p.required:
            required.append(p.name)
    if not cmd.read_only:
        props["model"] = {"type": "string", "description": "Your model name, recorded with you as the author of this write."}
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def tools() -> list[dict]:
    out = []
    for cmd in REGISTRY.values():
        if not cmd.mcp:
            continue
        description = " ".join(x for x in (cmd.summary, cmd.description) if x)
        out.append({
            "name": cmd.name,
            "title": cmd.summary.split(":")[0].rstrip("."),
            "description": description,
            "inputSchema": _input_schema(cmd),
            "annotations": {"readOnlyHint": cmd.read_only, "destructiveHint": False, "openWorldHint": False},
        })
    return out


class Server:
    def __init__(self) -> None:
        self.protocol = LATEST
        self.client = {"name": "mcp-client"}
        self.ctx: Context | None = None

    def _context(self, model: str | None) -> Context:
        actor = {"kind": "agent", "id": str(self.client.get("name") or "mcp-client")[:200]}
        model = model or os.environ.get("BYOBOT_AGENT_MODEL")
        if model:
            actor["model"] = str(model)[:200]
        if self.ctx is None:
            self.ctx = Context(actor=actor, path=db_path())
        self.ctx.actor = actor
        return self.ctx

    def handle(self, message: Any) -> dict | list | None:
        if isinstance(message, list):
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return replies or None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "invalid request")
        if "id" not in message:
            return None  # a notification, such as notifications/initialized
        mid, method, params = message["id"], message["method"], message.get("params") or {}
        try:
            if method == "initialize":
                return _result(mid, self._initialize(params))
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": tools()})
            if method == "tools/call":
                return self._call(mid, params)
            if method == "prompts/list":
                return _result(mid, {"prompts": [{"name": n, **p} for n, p in prompts.PROMPTS.items()]})
            if method == "prompts/get":
                return self._prompt(mid, params)
            return _error(mid, METHOD_NOT_FOUND, f"method not found: {method}")
        except Exception as exc:  # never let one request kill the server
            print(f"byobot mcp: {method} failed: {exc!r}", file=sys.stderr)
            return _error(mid, -32603, f"internal error: {exc}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        self.protocol = requested if requested in SUPPORTED_VERSIONS else LATEST
        if isinstance(params.get("clientInfo"), dict):
            self.client = params["clientInfo"]
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
            "serverInfo": {"name": "byobot", "title": "byobot", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, mid: Any, params: dict) -> dict:
        name = params.get("name")
        cmd = REGISTRY.get(name) if isinstance(name, str) else None
        if cmd is None or not cmd.mcp:
            return _error(mid, INVALID_PARAMS, f"unknown tool: {name}")
        args = dict(params.get("arguments") or {})
        model = args.pop("model", None) if not cmd.read_only else None
        try:
            result = run(cmd.name, self._context(model), args)
        except ByobotError as exc:
            body = exc.to_dict()
            return _result(mid, {"content": [{"type": "text", "text": json.dumps(body, indent=2)}], "isError": True})
        out: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
        if isinstance(result, dict) and self.protocol >= "2025-06-18":
            out["structuredContent"] = result
        out["isError"] = cmd.name == "validate" and not result["valid"]
        return _result(mid, out)

    def _prompt(self, mid: Any, params: dict) -> dict:
        name = params.get("name")
        if name not in prompts.PROMPTS:
            return _error(mid, INVALID_PARAMS, f"unknown prompt: {name}")
        text = prompts.render(name, params.get("arguments") or {})
        return _result(mid, {"description": prompts.PROMPTS[name]["description"],
                             "messages": [{"role": "user", "content": {"type": "text", "text": text}}]})

    def close(self) -> None:
        if self.ctx is not None:
            self.ctx.close()


def _result(mid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server()
    print(f"byobot mcp {__version__}: serving {db_path()} on stdio", file=sys.stderr)
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                reply: Any = _error(None, PARSE_ERROR, f"parse error: {exc}")
            else:
                reply = server.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0
