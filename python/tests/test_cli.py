import json
import subprocess
import sys

import pytest
from conftest import case

from byobot import __version__
from byobot.cli import build_parser, main
from byobot.commands import REGISTRY


def cli(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def test_cli_commands_come_from_the_registry():
    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert set(sub.choices) == {c.name.split(" ")[0] for c in REGISTRY.values() if c.cli} | {"mcp", "serve", "remote"}


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


def test_log_list_and_query(capsys):
    cli(capsys, "log", "activity.defined", json.dumps({"id": "plank", "name": "Plank", "default_metrics": ["duration"]}))
    code, out, _ = cli(capsys, "log", "effort.performed",
                       json.dumps({"activity_id": "plank", "metrics": {"duration": {"value": 60, "unit": "s"}}}))
    assert code == 0
    event = out["event"]
    assert event["actor"] == {"kind": "human", "id": "me"}
    assert event["payload"]["id"] and event["payload"]["performed_at"]  # filled in for us

    code, listed, _ = cli(capsys, "list", "efforts")
    assert listed["count"] == 1 and listed["items"][0]["metrics"] == {"duration": {"value": 60, "unit": "s"}}
    assert cli(capsys, "list", "efforts", "--activity", "row")[1]["count"] == 0
    assert cli(capsys, "list", "activities")[1]["items"][0]["default_metrics"] == ["duration"]

    code, rows, _ = cli(capsys, "query", "SELECT count(*) AS n FROM events")
    assert rows["rows"] == [{"n": 2}]


def test_planned_session_fills_ids_and_lists_nested(capsys):
    for id, name in (("pull_up", "Pull-Up"), ("dip", "Dip")):
        cli(capsys, "log", "activity.defined", json.dumps({"id": id, "name": name}))
    session = {"scheduled_on": "2026-11-23", "name": "Upper", "blocks": [
        {"rounds": 3, "metrics": {"rest": {"value": 90, "unit": "s"}}, "efforts": [
            {"activity_id": "pull_up", "metrics": {"reps": {"min": 6, "max": 8}}},
            {"activity_id": "dip", "metrics": {"reps": {"value": 10}}}]}]}
    code, out, _ = cli(capsys, "log", "session.planned", json.dumps(session))
    assert code == 0
    block = out["event"]["payload"]["blocks"][0]
    assert block["id"] and all(e["id"] for e in block["efforts"])
    planned = cli(capsys, "list", "planned")[1]["items"][0]
    assert planned["blocks"][0]["rounds"] == 3
    assert planned["blocks"][0]["metrics"] == {"rest": {"value": 90, "unit": "s"}}
    assert [e["activity_id"] for e in planned["blocks"][0]["efforts"]] == ["pull_up", "dip"]


def test_performed_session_with_efforts(capsys):
    code, out, _ = cli(capsys, "log", "session.performed", json.dumps({
        "name": "Tennis", "metrics": {"duration": {"value": 90, "unit": "min"}},
        "efforts": [{"activity_id": "tennis"}]}))
    assert code == 0 and out["event"]["payload"]["started_at"]
    session = cli(capsys, "list", "sessions")[1]["items"][0]
    assert session["metrics"]["duration"] == {"value": 90, "unit": "min"}
    assert session["efforts"][0]["activity_id"] == "tennis"


def test_log_from_file_and_stdin(capsys, tmp_path, monkeypatch):
    path = tmp_path / "ex.json"
    path.write_text(json.dumps({"id": "plank", "name": "Plank"}))
    assert cli(capsys, "log", "activity.defined", f"@{path}")[0] == 0
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({"id": "row", "name": "Row"})))
    assert cli(capsys, "log", "activity.defined", "-")[0] == 0
    assert cli(capsys, "list", "activities")[1]["count"] == 2


def test_actor_flag(capsys):
    code, out, _ = cli(capsys, "--actor", "agent:claude-code:example-model-1", "log", "activity.defined",
                       json.dumps({"id": "plank", "name": "Plank"}))
    assert out["event"]["actor"] == {"kind": "agent", "id": "claude-code", "model": "example-model-1"}


def test_actor_env(capsys, monkeypatch):
    monkeypatch.setenv("BYOBOT_ACTOR", "connector:whoop")
    code, out, _ = cli(capsys, "log", "activity.defined", json.dumps({"id": "run", "name": "Run"}))
    assert out["event"]["actor"] == {"kind": "connector", "id": "whoop"}


def test_bad_actor(capsys):
    code, out, err = cli(capsys, "--actor", "robot", "status")
    assert code == 1 and json.loads(err)["error"] == "invalid"


def test_invalid_log_exits_1_with_errors(capsys):
    code, out, err = cli(capsys, "log", "effort.performed", json.dumps({"activity_id": "plank", "metrics": {"reps": {"value": 5, "unit": "kg"}}}))
    assert code == 1 and out is None
    body = json.loads(err)
    assert body["error"] == "invalid" and body["details"]


def test_undo(capsys):
    _, out, _ = cli(capsys, "log", "activity.defined", json.dumps({"id": "plank", "name": "Plank"}))
    code, undo, _ = cli(capsys, "undo", out["event"]["id"], "--reason", "typo")
    assert code == 0 and undo["event"]["payload"] == {"event_id": out["event"]["id"], "reason": "typo"}
    assert cli(capsys, "list", "activities")[1]["count"] == 0
    events = cli(capsys, "list", "events")[1]["items"]
    assert events[1]["undone_by"] == undo["event"]["id"]


def test_ingest_ndjson_file(capsys, tmp_path):
    path = tmp_path / "samples.ndjson"
    path.write_text("\n".join(json.dumps(case(n)) for n in ("telemetry/body-mass", "telemetry/resting-hr")) + "\n")
    code, out, _ = cli(capsys, "ingest", f"@{path}")
    assert out == {"inserted": 2, "updated": 0, "unchanged": 0}
    assert cli(capsys, "ingest", f"@{path}")[1] == {"inserted": 0, "updated": 0, "unchanged": 2}
    code, listed, _ = cli(capsys, "list", "telemetry", "--type", "HKQuantityTypeIdentifierBodyMass")
    assert listed["count"] == 1


def test_validate(capsys):
    code, out, _ = cli(capsys, "validate", json.dumps(case("event/activity-defined")))
    assert code == 0 and out["valid"]
    code, out, _ = cli(capsys, "validate", "--as", "telemetry", json.dumps({"source": "x"}))
    assert code == 1 and not out["valid"] and out["errors"]
    code, out, _ = cli(capsys, "validate", "--as", "activity.defined", json.dumps({"id": "plank", "name": "Plank"}))
    assert code == 0 and out["valid"]


def test_schema(capsys):
    assert "event" in cli(capsys, "schema")[1]["schemas"]
    assert cli(capsys, "schema", "event")[1]["title"].startswith("byobot event")
    ddl = cli(capsys, "schema", "sql")[1]["ddl"]
    assert "CREATE VIEW plan_vs_actual" in ddl and "CREATE VIEW strength_sets" in ddl


def test_rebuild_and_status(capsys):
    cli(capsys, "log", "activity.defined", json.dumps({"id": "plank", "name": "Plank"}))
    assert cli(capsys, "rebuild")[1]["ledger"]["applied"] == 1
    assert cli(capsys, "status")[1]["counts"]["activities"] == 1


def test_bad_json_argument(capsys):
    code, _, err = cli(capsys, "log", "activity.defined", "{not json")
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


def test_correct_changes_only_what_you_name(capsys):
    cli(capsys, "init")
    code, out, _ = cli(capsys, "log", "effort.performed",
                       '{"id": "set-1", "activity_id": "pull_up", "performed_at": "2026-11-21T07:00:00-05:00", '
                       '"round": 2, "side": "both", "metrics": {"reps": {"value": 8}}}')
    assert code == 0
    code, out, _ = cli(capsys, "correct", "set-1", '{"metrics": {"reps": {"value": 7}}, "side": null, "notes": "last rep didn\'t count"}')
    assert code == 0, out
    effort = out["effort"]
    assert effort == {"id": "set-1", "activity_id": "pull_up", "performed_at": "2026-11-21T07:00:00-05:00", "round": 2,
                      "metrics": {"reps": {"value": 7}}, "notes": "last rep didn't count"}
    assert out["event"]["type"] == "effort.performed" and out["event"]["payload"] == effort
    code, out, _ = cli(capsys, "list", "efforts")
    assert [(e["id"], e["round"]) for e in out["items"]] == [("set-1", 2)]


def test_correct_refuses_unknown_fields_and_missing_efforts(capsys):
    cli(capsys, "init")
    code, out, err = cli(capsys, "correct", "missing", '{"notes": "x"}')
    assert code != 0
    cli(capsys, "log", "effort.performed", '{"id": "set-2", "activity_id": "plank"}')
    code, out, err = cli(capsys, "correct", "set-2", '{"id": "other"}')
    assert code != 0


def test_import_activities_keeps_yours_unless_told(capsys):
    from conftest import FIXTURES

    library = FIXTURES.parent / "library.json"
    cli(capsys, "init")
    cli(capsys, "log", "activity.defined", '{"id": "pull_up", "name": "Strict pull-up"}')
    code, out, _ = cli(capsys, "import", "activities", f"@{library}")
    assert code == 0, out
    assert "pull_up" in out["kept"] and len(out["added"]) == 13 and out["replaced"] == []
    code, out, _ = cli(capsys, "query", "SELECT name, targets FROM activities WHERE id IN ('pull_up', 'lat_pulldown') ORDER BY id")
    assert [r["name"] for r in out["rows"]] == ["Lat pulldown", "Strict pull-up"]
    code, out, _ = cli(capsys, "import", "activities", f"@{library}", "--existing", "replace")
    assert out["replaced"] == ["pull_up"] and out["added"] == [] and len(out["kept"]) == 13
    code, out, _ = cli(capsys, "import", "activities", '[{"id": "x", "name": "X", "targets": {"primary": ["lats"]}}]')
    assert code != 0


def test_log_and_list_locations(capsys):
    code, out, _ = cli(capsys, "log", "location.defined", json.dumps({"id": "home", "name": "Home gym"}))
    assert code == 0 and out["event"]["payload"] == {"id": "home", "name": "Home gym"}
    assert [i["name"] for i in cli(capsys, "list", "locations")[1]["items"]] == ["Home gym"]


def test_list_activities_by_source(capsys, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "activities").mkdir(parents=True)
    (lib / "vocabulary.json").write_text("{}")
    (lib / "activities" / "dip.json").write_text(json.dumps({"id": "dip", "name": "Dip", "targets": {"main": ["triceps"]}}))
    monkeypatch.setenv("BYOBOT_LIBRARY", str(lib))
    cli(capsys, "log", "activity.defined", '{"id": "plank", "name": "Plank"}')
    items = cli(capsys, "list", "activities")[1]["items"]
    assert [(i["id"], i["source"]) for i in items] == [("dip", "library"), ("plank", "ledger")]
    assert items[0]["targets"] == {"main": ["triceps"]} and "payload" not in items[0]
    assert [i["id"] for i in cli(capsys, "list", "activities", "--source", "library")[1]["items"]] == ["dip"]


def test_planned_session_must_name_existing_records(capsys):
    for id, name in (("barbell_back_squat", "Barbell Back Squat"), ("hack_squat", "Hack Squat")):
        cli(capsys, "log", "activity.defined", json.dumps({"id": id, "name": name}))
    session = {"scheduled_on": "2026-11-23", "name": "Legs", "location_id": "example-gym", "plan_id": "p1",
               "blocks": [{"efforts": [{"activity_id": "barbell_back_squat"}, {"activity_id": "back_squat"}]}]}
    code, out, err = cli(capsys, "log", "session.planned", json.dumps(session))
    assert code != 0
    details = json.loads(err)["details"]
    assert details == [{"kind": "activity", "id": "back_squat", "did_you_mean": ["barbell_back_squat", "hack_squat"]},
                       {"kind": "location", "id": "example-gym"}, {"kind": "plan", "id": "p1"}]
    assert cli(capsys, "query", "SELECT count(*) AS n FROM planned_sessions")[1]["rows"] == [{"n": 0}]

    cli(capsys, "log", "location.defined", json.dumps({"id": "example-gym", "name": "Example gym"}))
    cli(capsys, "log", "plan.defined", json.dumps({"id": "p1", "name": "Block", "starts_on": "2026-11-23"}))
    session["blocks"][0]["efforts"].pop()
    assert cli(capsys, "log", "session.planned", json.dumps(session))[0] == 0


def test_a_group_alone_shows_its_help(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["import"])
    assert exit.value.code == 0 and "activities" in capsys.readouterr().out
