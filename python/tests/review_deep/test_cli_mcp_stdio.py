"""Deep review (cli): `byobot mcp` answers every line it is sent and stays up; its replies are JSON."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import strict_loads, via_cli  # noqa: E402

from byobot import mcp  # noqa: E402

PING = b'{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n'


def _serve(lines: bytes, **env) -> tuple[list[dict], str]:
    proc = subprocess.run([sys.executable, "-m", "byobot", "mcp"], input=lines, capture_output=True, timeout=30,
                          env={**os.environ, **env})
    replies = [json.loads(line) for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    return replies, proc.stderr.decode("utf-8", "replace")


def test_cli_a_deeply_nested_line_is_a_parse_error_not_the_end_of_the_server():
    """CLI-7: json.loads raises RecursionError, which serve() doesn't catch, so the server exits and every later
    request from the agent goes unanswered."""
    replies, err = _serve(b"[" * 100_000 + b"]" * 100_000 + b"\n" + PING)
    assert [r.get("id") for r in replies] == [None, 2], err[-300:]
    assert replies[0]["error"]["code"] == mcp.PARSE_ERROR


def test_cli_a_line_that_is_not_utf8_is_a_parse_error_on_a_non_utf8_console():
    """CLI-7: on a console whose code page isn't UTF-8 (Windows' cp1252; simulated with PYTHONIOENCODING),
    utf8_stdio() reconfigures stdin to strict UTF-8, so one bad byte raises UnicodeDecodeError out of the read loop:
    the server dies before answering even the requests sent before that byte."""
    first = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
    replies, err = _serve(first + b"\xff\xfe\n" + PING, PYTHONIOENCODING="cp1252")
    assert [r.get("id") for r in replies] == [1, None, 2], err[-300:]


def test_cli_params_that_are_not_an_object_are_invalid_params():
    """CLI-7 (protocol): `params: [1]` or `arguments: [1]` is a caller mistake (-32602), not a server fault (-32603)."""
    server = mcp.Server()
    for params in ([1], {"name": "list", "arguments": [1]}, {"name": "list", "arguments": "kind=sessions"}):
        for method in ("tools/call", "initialize", "prompts/get"):
            reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "error" in reply:
                assert reply["error"]["code"] == mcp.INVALID_PARAMS, (method, params, reply)
    server.close()


def test_cli_a_query_result_is_always_json():
    """CLI-8: `SELECT 1e999` gives a float infinity, and json.dumps writes the bare word Infinity, which no strict
    JSON reader (JSON.parse, jq, serde) takes; over MCP the whole reply line fails to parse."""
    answer = via_cli(["query", "SELECT 1e999 AS x, -1e999 AS y"])
    assert answer[0] in ("ok", "error"), answer
    server = mcp.Server()
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "query", "arguments": {"sql": "SELECT 1e999 AS x"}}})
    server.close()
    strict_loads(json.dumps(reply, ensure_ascii=False))
