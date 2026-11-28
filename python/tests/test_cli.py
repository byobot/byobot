from __future__ import annotations

import contextlib
import http.client
import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from byobot import __version__, mcp, remote, serve
from byobot.cli import build_parser, main
from byobot.commands import REGISTRY, Command, Context, run
from byobot.core import commands as core
from byobot.core.store import Store
from byobot.review import review
from byobot.util import Conflict, ValidationFailed


def cli(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def test_cli_commands_come_from_the_registry():
    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert set(sub.choices) == {c.name.split(" ")[0] for c in REGISTRY.values() if c.cli}


def test_init_is_idempotent(capsys, byobot_home):
    code, first, _ = cli(capsys, "init")
    assert code == 0 and first["created"] is True and first["db"] == str(byobot_home / "byobot.db")
    code, second, _ = cli(capsys, "init")
    assert second["created"] is False and second["device_id"] == first["device_id"]


def test_home_flag_overrides_env(capsys, tmp_path):
    other = tmp_path / "elsewhere"
    code, out, _ = cli(capsys, "--home", str(other), "init")
    assert out["db"] == str(other / "byobot.db")


def test_profiles_keep_separate_stores(capsys, byobot_home):
    _, default, _ = cli(capsys, "init")
    _, alex, _ = cli(capsys, "--profile", "alex", "init")
    assert default["profile"] is None and alex["profile"] == "alex"
    assert alex["db"] == str(byobot_home / "profiles" / "alex" / "byobot.db")
    assert alex["device_id"] != default["device_id"]


def test_profile_comes_from_the_environment(capsys, byobot_home, monkeypatch):
    monkeypatch.setenv("BYOBOT_PROFILE", "sam")
    _, out, _ = cli(capsys, "init")
    assert out["db"] == str(byobot_home / "profiles" / "sam" / "byobot.db")


@pytest.mark.parametrize("name", ["Alex", "../x", "a b"])
def test_bad_profile_names_are_refused(capsys, name):
    code, _, err = cli(capsys, "--profile", name, "init")
    assert code == 1 and "invalid" in err


WEIGHT = {"source": "apple_health", "source_id": "w-1", "type": "HKQuantityTypeIdentifierBodyMass",
          "start": "2026-11-20T07:00:00-05:00", "end": "2026-11-20T07:00:00-05:00", "value": 82.4, "unit": "kg",
          "device": "Example scale"}
RESTING = {"source": "apple_health", "source_id": "r-1", "type": "HKQuantityTypeIdentifierRestingHeartRate",
           "start": "2026-11-20T00:00:00-05:00", "end": "2026-11-21T00:00:00-05:00", "value": 52,
           "unit": "count/min", "device": "Example Watch"}


def test_log_list_and_query(capsys):
    cli(capsys, "log", "activity", json.dumps({"name": "Plank", "default_metrics": ["duration"]}), "--id", "x.plank")
    code, out, _ = cli(capsys, "log", "effort",
                       json.dumps({"activity_id": "x.plank", "metrics": {"duration": {"value": 60, "unit": "s"}}}))
    assert code == 0
    event = out["events"][0]
    assert event["actor"] == {"kind": "human", "id": "me"} and event["record"]["kind"] == "effort"
    assert event["payload"]["time"]["start"]  # filled in for us

    code, listed, _ = cli(capsys, "list", "effort")
    assert listed["count"] == 1 and listed["items"][0]["metrics"] == {"duration": {"value": 60, "unit": "s"}}
    assert listed["items"][0]["id"] == event["record"]["id"]
    assert cli(capsys, "list", "effort", "--activity", "x.row")[1]["count"] == 0
    assert cli(capsys, "list", "activity")[1]["items"][0]["default_metrics"] == ["duration"]

    code, rows, _ = cli(capsys, "query", "SELECT count(*) AS n FROM events")
    assert rows["rows"] == [{"n": 2}]


def test_planned_session_fills_ids_and_lists_nested(capsys):
    for id, name in (("x.pull_up", "Pull-Up"), ("x.dip", "Dip")):
        cli(capsys, "log", "activity", json.dumps({"name": name}), "--id", id)
    session = {"scheduled_on": "2026-11-23", "name": "Upper", "blocks": [
        {"rounds": 3, "metrics": {"rest": {"value": 90, "unit": "s"}}, "efforts": [
            {"activity_id": "x.pull_up", "metrics": {"reps": {"min": 6, "max": 8}}},
            {"activity_id": "x.dip", "metrics": {"reps": {"value": 10}}}]}]}
    code, out, _ = cli(capsys, "log", "planned_session", json.dumps(session))
    assert code == 0
    rid = out["events"][0]["record"]["id"]
    block = out["events"][0]["payload"]["blocks"][0]
    assert block["id"] == f"{rid}.0" and [e["id"] for e in block["efforts"]] == [f"{rid}.0.0", f"{rid}.0.1"]
    planned = cli(capsys, "list", "planned_session")[1]["items"][0]
    assert planned["blocks"][0]["rounds"] == 3
    assert planned["blocks"][0]["metrics"] == {"rest": {"value": 90, "unit": "s"}}
    assert [e["activity_id"] for e in planned["blocks"][0]["efforts"]] == ["x.pull_up", "x.dip"]
    assert planned["blocks"][0]["efforts"][0]["metrics"] == {"reps": {"min": 6, "max": 8}}



def test_editing_a_planned_sessions_blocks_keeps_their_ids(capsys):
    for id, name in (("x.pull_up", "Pull-Up"), ("x.dip", "Dip")):
        cli(capsys, "log", "activity", json.dumps({"name": name}), "--id", id)
    session = {"scheduled_on": "2026-11-23", "name": "Upper", "blocks": [
        {"rounds": 3, "efforts": [{"activity_id": "x.pull_up"}, {"activity_id": "x.dip"}]}]}
    rid = cli(capsys, "log", "planned_session", json.dumps(session))[1]["events"][0]["record"]["id"]
    swapped = {"blocks": [{"rounds": 4, "efforts": [{"activity_id": "x.dip"}, {"activity_id": "x.pull_up"}]}]}
    code, out, _ = cli(capsys, "edit", "planned_session", rid, json.dumps(swapped))
    assert code == 0 and out["events"][0]["payload"]["name"] == "Upper"
    block = out["events"][0]["payload"]["blocks"][0]
    assert block["id"] == f"{rid}.0" and block["rounds"] == 4
    assert [e["id"] for e in block["efforts"]] == [f"{rid}.0.1", f"{rid}.0.0"]
    code, _, err = cli(capsys, "edit", "planned_session", rid, '{"blocks": [{"efforts": [{"activity_id": "x.nope"}]}]}')
    assert code == 1 and json.loads(err)["details"][0]["id"] == "x.nope"


S1 = "01a2147e-0000-7000-8000-000000000001"
P1 = "01a2147e-0000-7000-8000-000000000002"


def test_session_with_efforts_in_one_batch(capsys):
    batch = [{"kind": "session", "id": S1, "payload": {
                  "name": "Tennis", "time": {"start": "2026-11-20T18:00:00-05:00", "end": "2026-11-20T19:30:00-05:00"},
                  "metrics": {"duration": {"value": 90, "unit": "min"}}}},
             {"kind": "effort", "payload": {"activity_id": "tennis", "links": {"part_of": {"kind": "session", "id": S1}},
                                            "time": {"start": "2026-11-20T18:00:00-05:00"}}}]
    code, out, _ = cli(capsys, "log", "batch", json.dumps(batch))
    assert code == 0 and len(out["events"]) == 2
    session = cli(capsys, "list", "session")[1]["items"][0]
    assert session["metrics"]["duration"] == {"value": 90, "unit": "min"} and session["local_date"] == "2026-11-20"
    assert session["efforts"] == [out["events"][1]["record"]["id"]]


def test_log_from_file_and_stdin(capsys, tmp_path, monkeypatch):
    path = tmp_path / "ex.json"
    path.write_text(json.dumps({"name": "Plank"}))
    assert cli(capsys, "log", "activity", f"@{path}", "--id", "x.plank")[0] == 0
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({"name": "Row"})))
    assert cli(capsys, "log", "activity", "-", "--id", "x.row")[0] == 0
    assert cli(capsys, "list", "activity")[1]["count"] == 2


def test_actor_flag(capsys):
    code, out, _ = cli(capsys, "--actor", "agent:claude-code:example-model-1", "log", "activity",
                       json.dumps({"name": "Plank"}), "--id", "x.plank")
    assert out["events"][0]["actor"] == {"kind": "agent", "id": "claude-code", "model": "example-model-1"}


def test_actor_env(capsys, monkeypatch):
    monkeypatch.setenv("BYOBOT_ACTOR", "agent:example-coach")
    code, out, _ = cli(capsys, "log", "activity", json.dumps({"name": "Run"}), "--id", "x.run")
    assert out["events"][0]["actor"] == {"kind": "agent", "id": "example-coach"}
    monkeypatch.setenv("BYOBOT_ACTOR", "connector:whoop")
    assert cli(capsys, "log", "activity", json.dumps({"name": "Row"}), "--id", "x.row")[0] == 1  # sources write imports


def test_bad_actor(capsys):
    code, out, err = cli(capsys, "--actor", "robot", "status")
    assert code == 1 and json.loads(err)["error"] == "invalid"


def test_invalid_log_exits_1_with_errors(capsys):
    code, out, err = cli(capsys, "log", "effort",
                         json.dumps({"activity_id": "x.plank", "metrics": {"reps": {"value": 5, "unit": "kg"}}}))
    assert code == 1 and out is None
    assert json.loads(err)["error"] == "invalid"


def test_edit_and_undo(capsys):
    _, out, _ = cli(capsys, "log", "effort", json.dumps({"activity_id": "x.pull_up", "metrics": {"reps": {"value": 8}}}))
    rid = out["events"][0]["record"]["id"]
    code, edit, _ = cli(capsys, "edit", "effort", rid, '{"metrics": {"reps": {"value": 7}}, "notes": "last one didn\'t count"}')
    assert code == 0 and edit["events"][0]["prev"] == [out["events"][0]["id"]]
    effort = cli(capsys, "list", "effort")[1]["items"][0]
    assert effort["metrics"] == {"reps": {"value": 7}}
    code, undo, _ = cli(capsys, "undo", edit["events"][0]["id"])
    assert code == 0 and undo["events"][0]["reverts"] == edit["events"][0]["id"]
    assert cli(capsys, "list", "effort")[1]["items"][0]["metrics"] == {"reps": {"value": 8}}
    assert cli(capsys, "edit", "effort", "missing", '{"notes": "x"}')[0] != 0


def test_remove_and_versions(capsys):
    cli(capsys, "log", "activity", json.dumps({"name": "Plank"}), "--id", "x.plank")
    assert cli(capsys, "remove", "activity", "x.plank", "--reason", "typo")[0] == 0
    assert cli(capsys, "list", "activity")[1]["count"] == 0
    versions = cli(capsys, "versions", "activity", "x.plank")[1]["versions"]
    assert [v["op"] for v in versions] == ["put", "retract"]


def test_erase_a_session_with_its_efforts(capsys):
    session = cli(capsys, "log", "session", '{"name": "Legs"}')[1]["events"][0]["record"]["id"]
    effort = {"activity_id": "x.squat", "links": {"part_of": {"kind": "session", "id": session}}}
    cli(capsys, "log", "effort", json.dumps(effort))
    code, out, _ = cli(capsys, "erase", "session", session, "--efforts")
    assert code == 0 and [e["record"]["kind"] for e in out["events"]] == ["session", "effort"]
    assert cli(capsys, "list", "effort")[1]["count"] == 0


def test_ingest_ndjson_file(capsys, tmp_path):
    path = tmp_path / "samples.ndjson"
    path.write_text("\n".join(json.dumps(s) for s in (WEIGHT, RESTING)) + "\n")
    code, out, _ = cli(capsys, "ingest", f"@{path}")
    assert out == {"written": {"apple_health": 2}, "unchanged": 0}
    assert cli(capsys, "ingest", f"@{path}")[1] == {"written": {"apple_health": 0}, "unchanged": 2}
    code, listed, _ = cli(capsys, "list", "observation", "--type", "body_mass")
    assert listed["count"] == 1 and listed["items"][0]["value"] == 82.4


def test_correct_and_exclude_an_import(capsys):
    cli(capsys, "ingest", json.dumps(WEIGHT))
    rid = cli(capsys, "list", "source_record")[1]["items"][0]["id"]
    code, out, _ = cli(capsys, "correct", rid, '{"value": 81.9, "unit": "kg"}')
    assert code == 0, out
    assert cli(capsys, "list", "observation")[1]["items"][0]["value"] == 81.9
    assert cli(capsys, "exclude", rid, "--reason", "wrong person")[0] == 0
    assert cli(capsys, "list", "observation")[1]["count"] == 0
    assert cli(capsys, "correct", "missing", '{"value": 1}')[0] != 0


def test_validate(capsys):
    code, out, _ = cli(capsys, "validate", "activity", json.dumps({"name": "Plank"}))
    assert code == 0 and out == {"valid": True, "kind": "activity", "errors": []}
    code, out, _ = cli(capsys, "validate", "observation", json.dumps({"code": "body_mass"}))
    assert code == 0 and not out["valid"] and out["errors"]
    code, out, _ = cli(capsys, "validate", "event", json.dumps({"id": "x"}))
    assert code == 0 and not out["valid"]
    batch = [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}},
             {"kind": "observation", "payload": {"code": "body_mass"}}]
    out = cli(capsys, "validate", "batch", json.dumps(batch))[1]
    assert not out["valid"] and [e.split(":")[0] for e in out["errors"]] == ["1"]
    assert cli(capsys, "validate", "batch", json.dumps(batch[:1]))[1]["valid"]


def test_schema(capsys):
    assert "event" in cli(capsys, "schema")[1]["schemas"]
    assert cli(capsys, "schema")[1]["schema_version"] == 1
    assert cli(capsys, "schema", "event")[1]["title"]
    assert "body_mass" in cli(capsys, "schema", "registry")[1]["codes"]
    ddl = cli(capsys, "schema", "sql")[1]["ddl"]
    assert "CREATE VIEW IF NOT EXISTS plan_vs_actual" in ddl and "CREATE TABLE IF NOT EXISTS efforts" in ddl


def test_rebuild_and_status(capsys):
    cli(capsys, "log", "activity", json.dumps({"name": "Plank"}), "--id", "x.plank")
    assert cli(capsys, "rebuild")[1]["counts"]["activities"] == 1
    assert cli(capsys, "status")[1]["counts"]["events"] == 1


def test_bad_json_argument(capsys):
    code, _, err = cli(capsys, "log", "activity", "{not json", "--id", "x.plank")
    assert code == 1 and "could not read JSON" in json.loads(err)["message"]


def test_bare_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage: byobot" in capsys.readouterr().out


def test_module_entry_point_and_version():
    out = subprocess.run([sys.executable, "-m", "byobot", "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == f"byobot {__version__}"


@pytest.mark.parametrize("tier", ["truecolor", "256", "mono", "ascii"])
def test_mascot_renders(tier):
    from byobot import mascot

    art = mascot.render(t=tier)
    assert art and all(isinstance(line, str) for line in art)


LIBRARY = [
    {"id": "x.pull_up", "name": "Pull-up", "targets": {"main": ["lats"], "synergists": ["biceps"]}, "equipment": ["pull_up_bar"]},
    {"id": "x.lat_pulldown", "name": "Lat pulldown", "targets": {"main": ["lats"], "synergists": []}},
]


def test_import_activities_keeps_yours_unless_told(capsys, tmp_path):
    library = tmp_path / "library.json"
    library.write_text(json.dumps({"activities": LIBRARY}))
    cli(capsys, "log", "activity", '{"name": "Strict pull-up"}', "--id", "x.pull_up")
    code, out, _ = cli(capsys, "import", "activities", f"@{library}")
    assert code == 0, out
    assert out == {"added": ["x.lat_pulldown"], "replaced": [], "kept": ["x.pull_up"]}
    code, out, _ = cli(capsys, "query", "SELECT name FROM activities ORDER BY id")
    assert [r["name"] for r in out["rows"]] == ["Lat pulldown", "Strict pull-up"]
    code, out, _ = cli(capsys, "import", "activities", f"@{library}", "--existing", "replace")
    assert out == {"added": [], "replaced": ["x.pull_up"], "kept": ["x.lat_pulldown"]}
    code, out, _ = cli(capsys, "import", "activities", '[{"id": "x", "name": "X", "targets": {"primary": ["lats"]}}]')
    assert code != 0
    assert cli(capsys, "query", "SELECT count(*) AS n FROM activities")[1]["rows"] == [{"n": 2}]
    assert cli(capsys, "import", "activities", f"@{library}", "--days", "3")[0] == 1
    assert cli(capsys, "import", "whoop", f"@{library}")[0] == 1


def test_log_and_list_locations(capsys):
    code, out, _ = cli(capsys, "log", "location", json.dumps({"name": "Home gym"}), "--id", "home")
    assert code == 0 and out["events"][0]["record"] == {"kind": "location", "id": "home"}
    assert [i["name"] for i in cli(capsys, "list", "location")[1]["items"]] == ["Home gym"]


def test_list_activities_by_source(capsys, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "activities").mkdir(parents=True)
    (lib / "vocabulary.json").write_text("{}")
    (lib / "activities" / "dip.json").write_text(json.dumps({"name": "Dip", "targets": {"main": ["triceps"]}}))
    monkeypatch.setenv("BYOBOT_LIBRARY", str(lib))
    cli(capsys, "log", "activity", '{"name": "Plank"}', "--id", "x.plank")
    items = cli(capsys, "list", "activity")[1]["items"]
    assert [(i["id"], i["source"]) for i in items] == [("dip", "library"), ("x.plank", "store")]
    assert items[0]["targets"] == {"main": ["triceps"]} and "payload" not in items[0]
    assert [i["id"] for i in cli(capsys, "list", "activity", "--source", "library")[1]["items"]] == ["dip"]


def test_list_refuses_filters_that_dont_fit_and_says_when_cut_short(capsys):
    code, _, err = cli(capsys, "list", "session", "--muscle", "chest")
    assert code == 1 and json.loads(err)["message"] == "muscle doesn't apply to list session; it fits activity"
    assert cli(capsys, "list", "location", "--detail", "brief")[0] == 1
    for i in range(3):
        cli(capsys, "log", "location", json.dumps({"name": f"Gym {i}"}), "--id", f"gym_{i}")
    out = cli(capsys, "list", "location", "--limit", "2")[1]
    assert out["count"] == 2 and out["truncated"] is True
    assert cli(capsys, "list", "location", "--limit", "3")[1]["truncated"] is False


def test_list_one_record_by_id(capsys):
    cli(capsys, "log", "location", '{"name": "Example gym"}', "--id", "example_gym")
    cli(capsys, "log", "location", '{"name": "Home gym"}', "--id", "home")
    assert [i["id"] for i in cli(capsys, "list", "location", "--id", "home")[1]["items"]] == ["home"]
    event = cli(capsys, "list", "events", "--limit", "1")[1]["items"][0]
    assert cli(capsys, "list", "events", "--id", event["id"])[1]["items"] == [event]


def test_planned_session_must_name_existing_records(capsys):
    for id, name in (("x.barbell_back_squat", "Barbell Back Squat"), ("x.hack_squat", "Hack Squat")):
        cli(capsys, "log", "activity", json.dumps({"name": name}), "--id", id)
    session = {"scheduled_on": "2026-11-23", "name": "Legs", "location_id": "example_gym",
               "links": {"part_of": {"kind": "plan", "id": P1}},
               "blocks": [{"efforts": [{"activity_id": "x.barbell_back_squat"}, {"activity_id": "back_squat"}]}]}
    code, out, err = cli(capsys, "log", "planned_session", json.dumps(session))
    assert code != 0
    details = json.loads(err)["details"]
    assert details == [{"kind": "activity", "id": "back_squat", "did_you_mean": ["x.barbell_back_squat", "x.hack_squat"]},
                       {"kind": "location", "id": "example_gym"}, {"kind": "plan", "id": P1}]
    assert cli(capsys, "query", "SELECT count(*) AS n FROM planned_sessions")[1]["rows"] == [{"n": 0}]

    session["blocks"][0]["efforts"].pop()
    batch = [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}},
             {"kind": "plan", "id": P1, "payload": {"name": "Block", "starts_on": "2026-11-23"}},
             {"kind": "planned_session", "payload": session}]
    assert cli(capsys, "log", "batch", json.dumps(batch))[0] == 0


def test_a_group_alone_shows_its_help(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["devices"])
    assert exit.value.code == 0 and "remove" in capsys.readouterr().out


def test_your_own_activity_ids_start_with_x(capsys):
    code, _, err = cli(capsys, "log", "activity", json.dumps({"name": "Plank"}), "--id", "plank")
    assert code == 1 and "x.plank" in json.loads(err)["message"]
    assert cli(capsys, "log", "activity", json.dumps({"name": "Plank"}), "--id", "x.plank")[0] == 0


def test_switches_are_booleans():
    assert build_parser().parse_args(["serve", "--no-pair"]).pair is False
    assert build_parser().parse_args(["remote", "key", "example", "--read-only"]).read_only is True
    assert build_parser().parse_args(["serve"]).pair is None
    with pytest.raises(ValidationFailed, match="true or false"):
        run("remote key", Context(actor={"kind": "human", "id": "me"}), {"name": "example", "show": "yes"})


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

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_cli_mcp_does_not_offer_at_path_files_it_cannot_read(tmp_path):
    """CLI-11: import activities tells agents "Use @path for a file", but only the CLI reads @path; over MCP and the
    API the string is refused as "expected a list of activities"."""
    path = tmp_path / "activities.json"
    path.write_text(json.dumps([{"id": "x_example_carry", "name": "Example carry"}]))
    tool = next(t for t in mcp.tools() if t["name"] == "import")
    if "@path" in json.dumps(tool):
        assert via_mcp("import", {"source": "activities", "records": f"@{path}"})[0] == "ok"


def test_cli_a_text_value_is_taken_as_the_help_says(capsys):
    """CLI-11: `resolve` takes an event id or "remove", but it is a JSON argument on the CLI, so
    `byobot resolve session ID remove` stops at "could not read JSON" unless the word is typed with JSON quotes."""
    via_cli(["resolve", "session", "example-id", "remove"])
    assert "could not read JSON" not in capsys.readouterr().err


ME = {"kind": "human", "id": "me"}


EST = timezone(timedelta(hours=-5))


@pytest.mark.parametrize("bound", ["2026/11/20", "11/20/2026", "20.11.2026", "garbage123"])
@pytest.mark.parametrize("which", ["since", "until"])
def test_cli_a_date_bound_that_is_not_a_date_is_refused(bound, which):
    """CLI-4: one session on 2026-11-25; `--since 2026/11/20` lists none and `--since 11/20/2026` lists it, both
    without a word, so an agent reports "no sessions" or the wrong ones."""
    assert via_cli(["log", "session", '{"name": "Example session", "time": {"start": "2026-11-25T10:00:00-05:00"}}'])[0] \
        == "ok"
    assert via_cli(["list", "session", f"--{which}", bound]) == ("error", "invalid")
    assert via_mcp("list", {"kind": "observation", which: bound}) == ("error", "invalid")


@pytest.mark.parametrize("bound", ["yesterday", "2026-13-45T99:00:00Z"])
def test_cli_an_events_bound_that_is_not_a_time_is_refused(bound):
    """CLI-4: `list events` never checks its bounds."""
    assert via_cli(["init"])[0] == "ok"
    assert via_cli(["list", "events", "--since", bound]) == ("error", "invalid")


def test_cli_events_since_a_timestamp_compares_instants_not_text(byobot_home):
    """CLI-4: an event written at 10:00-05:00 (15:00Z) is after 14:00Z and before 16:00Z, but `ts >= '…14:00:00Z'`
    compares "10:00…" with "14:00…" as text: it is left out of `--since 14:00Z` and kept by `--until 12:00Z`."""
    with Store(byobot_home / "byobot.db", wall=lambda: datetime(2026, 11, 20, 10, 0, tzinfo=EST)) as store:
        core.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    ctx = Context(actor=ME, path=byobot_home / "byobot.db")
    try:
        since = run("list", ctx, {"kind": "events", "type": "location", "since": "2026-11-20T14:00:00Z"})
        until = run("list", ctx, {"kind": "events", "type": "location", "until": "2026-11-20T12:00:00Z"})
    finally:
        ctx.close()
    assert (since["count"], until["count"]) == (1, 0)


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


@pytest.mark.parametrize("module,saved_in", [(serve, serve.serve_dir), (remote, remote.remote_dir)],
                         ids=["serve", "remote"])
def test_cli_two_profiles_never_keep_the_same_port(module, saved_in, monkeypatch):
    """CLI-5: alex runs `byobot serve` (or `remote`) first and keeps port 8767 (8781). Later, while alex's server is
    stopped, sam runs it for the first time: the port is free, so sam keeps the same one. From then on the two can't
    serve at once ("could not listen"), and alex's tunnel (`tailscale funnel ... 8781`) reaches sam's server."""
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    alex = module.choose_port()
    saved_in().mkdir(parents=True, exist_ok=True)
    (saved_in() / "port").write_text(f"{alex}\n")  # what run() writes once it listens
    monkeypatch.setenv("BYOBOT_PROFILE", "sam")
    sam = module.choose_port()
    assert sam != alex


def test_cli_a_mistyped_profile_is_refused_not_created(byobot_home):
    """CLI-6: `byobot --profile alxe ...` (or an MCP config with that typo) quietly makes a new empty store: reads
    say there is nothing, and writes land in a store nobody looks at. Restore only fills an empty store, so they
    can't be moved back."""
    assert via_cli(["--profile", "alex", "init"])[0] == "ok"
    assert via_cli(["--profile", "alex", "log", "location", '{"name": "Example gym"}', "--id", "example_gym"])[0] == "ok"
    read = via_cli(["--profile", "alxe", "list", "location"])
    write = via_cli(["--profile", "alxe", "log", "observation", '{"code": "body_mass", "value": 80, "unit": "kg"}'])
    assert (read[0], write[0]) == ("error", "error")
    assert not (byobot_home / "profiles" / "alxe").exists()


HOLD_S = 6.5  # longer than the store's 5 s busy timeout, as a big ingest, restore or rebuild is


def test_cli_a_read_answers_while_another_process_holds_the_write_lock(byobot_home):
    """CLI-13."""
    assert via_cli(["init"])[0] == "ok"
    ready = threading.Event()

    def writer():
        conn = sqlite3.connect(byobot_home / "byobot.db", isolation_level=None, timeout=10)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO meta VALUES ('example_long_write', '1') ON CONFLICT (key) DO NOTHING")
        ready.set()
        time.sleep(HOLD_S)
        conn.execute("COMMIT")
        conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    ready.wait(5)
    try:
        cli_answer = via_cli(["list", "session"])
        mcp_answer = via_mcp("list", {"kind": "session"})
    finally:
        thread.join()
    assert (cli_answer[0], mcp_answer[0]) == ("ok", "ok"), (cli_answer, mcp_answer)


def _planned(day: str, status: str | None) -> dict:
    payload = {"scheduled_on": day, "name": f"Example {status or 'planned'} day", "location_id": "example_gym",
               "blocks": [{"efforts": [{"activity_id": "x.example_press"}]}]}
    if status:
        payload["status"] = status
    return {"kind": "planned_session", "payload": payload}


def test_cli_a_cancelled_session_is_not_planned_or_missed(byobot_home):
    """CLI-12: an agent that cancels Tuesday's session (status cancelled) then reads `review` is told the person
    missed it, and adherence counts it as planned."""
    with Store(byobot_home / "byobot.db") as store:
        core.log(store, ME, [_planned("2026-11-17", "cancelled"), _planned("2026-11-18", None),
                             _planned("2026-11-19", "skipped")])
        out = review(store, 2, "2026-11-23")
    assert out["plan_adherence"]["planned"] == 1
    assert [m["scheduled_on"] for m in out["plan_adherence"]["missed"]] == ["2026-11-18"]


@pytest.mark.parametrize("make,argv", [
    (lambda tmp: None, ["log", "session", "@{tmp}/no-such-file.json"]),
    (lambda tmp: (tmp / "a-folder").mkdir(), ["log", "session", "@{tmp}/a-folder"]),
    (lambda tmp: (tmp / "latin1.json").write_bytes(b'{"notes": "caf\xe9"}'), ["log", "session", "@{tmp}/latin1.json"]),
    (lambda tmp: None, ["export", "{tmp}/no-such-folder/backup.jsonl"]),
    (lambda tmp: None, ["review", "--until", "9999-12-31"]),
    (lambda tmp: None, ["review", "--until", "0001-01-07"]),
    (lambda tmp: None, ["review", "--weeks", "1000000"]),
    (lambda tmp: None, ["log", "observation",
                        '{{"code": "body_mass", "value": 80, "unit": "kg", "time": {{"start": "9999-12-31T23:30:00"}}}}']),
    (lambda tmp: None, ["validate", "event", "[1, 2]"]),
    (lambda tmp: (tmp / "a-file").write_text("x"), ["--home", "{tmp}/a-file", "status"]),
    (lambda tmp: (tmp / "cut.jsonl").write_text('{"byobot_export": 1}\n'), ["restore", "{tmp}/cut.jsonl"]),
], ids=["@missing", "@folder", "@not-utf8", "export-missing-folder", "review-until-9999", "review-until-0001",
        "review-weeks-huge", "observation-year-9999", "validate-a-list", "home-is-a-file",
        "restore-header-without-id"])
def test_cli_a_mistake_is_a_json_error_not_a_traceback(make, argv, tmp_path):
    """CLI-3."""
    make(tmp_path)
    answer = via_cli([a.format(tmp=tmp_path) for a in argv])
    assert answer[0] in ("error", "ok"), answer


@pytest.mark.parametrize("tool,args", [
    ("review", {"until": "9999-12-31"}),
    ("review", {"weeks": 1000000}),
    ("validate", {"kind": "event", "document": [1, 2]}),
    ("log", {"kind": "observation", "payload": {"code": "body_mass", "value": 80, "unit": "kg",
                                                "time": {"start": "9999-12-31T23:30:00"}}}),
])
def test_cli_mcp_answers_a_mistake_as_a_tool_error_not_an_internal_error(tool, args):
    """CLI-3: the same inputs over MCP are JSON-RPC -32603 "internal error", which agents treat as a server fault."""
    answer = via_mcp(tool, args)
    assert answer[0] != "broken", answer


def test_cli_a_closed_pipe_ends_quietly(monkeypatch):
    """CLI-9: `byobot list ... | head` that closes early must not print a BrokenPipeError traceback."""

    class Closed(io.StringIO):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", Closed())
    try:
        main(["schema"])
    except SystemExit:
        pass


def test_cli_ctrl_c_ends_with_130_and_no_traceback(monkeypatch, capsys):
    """CLI-9: Ctrl-C during a command (a long query, an import) should exit 130 quietly."""

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("byobot.cli.run", interrupted)
    try:
        code = main(["status"])
    except SystemExit as exc:
        code = exc.code
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped main(), so Python prints a traceback")
    assert code == 130
    assert "Traceback" not in capsys.readouterr().err


def test_cli_serve_with_a_port_out_of_range_is_a_json_error(capsys):
    """CLI-3: `byobot serve --port 70000` raises OverflowError, which run() doesn't catch (it catches OSError)."""
    try:
        code = main(["serve", "--port", "70000", "--no-pair"])
    except SystemExit as exc:
        code = exc.code
    except OverflowError as exc:
        pytest.fail(f"traceback: {exc!r}")
    err = capsys.readouterr().err
    assert code == 1 and "error" in json.loads(err)
