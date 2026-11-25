import io
import json
import subprocess
import sys

from byobot.commands import REGISTRY
from byobot.mcp import LATEST, Server, serve, tools


def call(server, mid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


def initialized(version=LATEST, client="test-agent"):
    server = Server()
    reply = call(server, 1, "initialize", {"protocolVersion": version, "capabilities": {},
                                           "clientInfo": {"name": client, "version": "1"}})
    assert reply["result"]["protocolVersion"] == version
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    return server


def tool(server, name, **arguments):
    reply = call(server, 2, "tools/call", {"name": name, "arguments": arguments})
    return reply["result"]


def test_tools_come_from_the_registry():
    assert {t["name"] for t in tools()} == {c.tool for c in REGISTRY.values() if c.mcp}
    by_name = {t["name"]: t for t in tools()}
    assert by_name["query"]["annotations"]["readOnlyHint"] is True
    assert by_name["log"]["annotations"]["readOnlyHint"] is False
    assert by_name["log"]["inputSchema"]["required"] == ["kind", "payload"]
    assert "model" in by_name["log"]["inputSchema"]["properties"]
    assert "model" not in by_name["query"]["inputSchema"]["properties"]


def test_initialize_negotiates_version():
    server = Server()
    reply = call(server, 1, "initialize", {"protocolVersion": "1999-01-01", "clientInfo": {"name": "x"}})
    assert reply["result"]["protocolVersion"] == LATEST
    assert reply["result"]["capabilities"] == {"tools": {"listChanged": False}, "prompts": {"listChanged": False}}
    assert reply["result"]["serverInfo"]["name"] == "byobot"


def test_writes_are_attributed_to_the_agent():
    server = initialized(client="claude-code")
    result = tool(server, "log", kind="activity", id="plank", payload={"name": "Plank"}, model="example-model-1")
    assert result["isError"] is False
    assert result["structuredContent"]["event"]["actor"] == {"kind": "agent", "id": "claude-code",
                                                             "model": "example-model-1"}
    rows = tool(server, "query", sql="SELECT actor_kind, actor_id, actor_model FROM events")
    assert rows["structuredContent"]["rows"] == [{"actor_kind": "agent", "actor_id": "claude-code",
                                                  "actor_model": "example-model-1"}]
    server.close()


def test_tool_errors_are_results_not_protocol_errors():
    server = initialized()
    result = tool(server, "log", kind="effort", payload={"activity_id": "plank", "metrics": {"x.Bad": {"value": 1}}})
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error"] == "invalid"
    result = tool(server, "query", sql="DELETE FROM events")
    assert result["isError"] is True
    server.close()


def test_agent_can_log_open_metrics_and_read_views():
    server = initialized(client="claude-code")
    tool(server, "log", kind="activity", id="squat", payload={"name": "Squat"})
    plan = {"scheduled_on": "2026-11-23", "name": "Lift", "blocks": [
        {"rounds": 3, "efforts": [{"activity_id": "squat",
                                   "metrics": {"reps": {"min": 3, "max": 5}, "load": {"value": 100, "unit": "kg"}}}]}]}
    planned = tool(server, "log", kind="planned_session", payload=plan)
    assert planned["isError"] is False
    pid = planned["structuredContent"]["event"]["record"]["id"]
    done = {"activity_id": "squat", "round": 1, "links": {"answers": {"kind": "planned_session", "id": pid,
                                                                      "part": f"{pid}.0.0"}},
            "metrics": {"reps": {"value": 5}, "load": {"value": 225, "unit": "[lb_av]"}, "x.tempo": {"value": "3010"}}}
    assert tool(server, "log", kind="effort", payload=done)["isError"] is False
    rows = tool(server, "query", sql="SELECT target_reps_max, actual_reps, round(actual_load_kg, 1) AS kg FROM plan_vs_actual")
    assert rows["structuredContent"]["rows"] == [{"target_reps_max": 5, "actual_reps": 5, "kg": 102.1}]
    server.close()


def test_validate_tool_sets_is_error_when_invalid():
    server = initialized()
    assert tool(server, "validate", document={"name": "Plank"}, **{"as": "activity"})["isError"] is False
    assert tool(server, "validate", document={}, **{"as": "observation"})["isError"] is True


def test_old_protocol_gets_no_structured_content():
    server = initialized(version="2024-11-05")
    result = tool(server, "schema")
    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"])["schema_version"] == 1


def test_protocol_errors():
    server = Server()
    assert call(server, 1, "resources/list")["error"]["code"] == -32601
    assert call(server, 2, "tools/call", {"name": "init"})["error"]["code"] == -32602  # CLI only
    assert server.handle({"id": 3})["error"]["code"] == -32600
    assert call(server, 4, "ping") == {"jsonrpc": "2.0", "id": 4, "result": {}}


def test_serve_over_streams():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": LATEST, "clientInfo": {"name": "t"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\nnot json\n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700


def test_byobot_mcp_subprocess(byobot_home):
    """End to end: `byobot mcp` as an agent would launch it."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": LATEST, "clientInfo": {"name": "e2e"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "ingest", "arguments": {"samples": {
             "source": "hume", "source_id": "w1", "type": "HKQuantityTypeIdentifierBodyMass",
             "start": "2026-11-20T07:12:00Z", "end": "2026-11-20T07:12:00Z", "value": 82.4, "unit": "kg"}}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list", "arguments": {"kind": "observations"}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "byobot", "mcp"], input="\n".join(json.dumps(m) for m in messages) + "\n",
                          capture_output=True, text=True, timeout=60, check=True)
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    assert replies[2]["result"]["structuredContent"] == {"written": {"hume": 1}, "unchanged": 0}
    assert replies[3]["result"]["structuredContent"]["items"][0]["value"] == 82.4
    assert (byobot_home / "byobot.db").exists()


def test_weekly_plan_prompt():
    server = initialized()
    listed = call(server, 2, "prompts/list")["result"]["prompts"]
    assert [p["name"] for p in listed] == ["weekly_plan"]
    got = call(server, 3, "prompts/get", {"name": "weekly_plan", "arguments": {"goal": "strength"}})["result"]
    text = got["messages"][0]["content"]["text"]
    assert "My goal: strength" in text and "`review`" in text and "{" in text
    assert call(server, 4, "prompts/get", {"name": "nope"})["error"]["code"] == -32602
    server.close()
