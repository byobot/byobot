import json
import subprocess
import sys

import pytest

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


WEIGHT = {"source": "hume", "source_id": "w-1", "type": "HKQuantityTypeIdentifierBodyMass",
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
    event = out["event"]
    assert event["actor"] == {"kind": "human", "id": "me"} and event["record"]["kind"] == "effort"
    assert event["payload"]["time"]["start"] and event["payload"]["modality"] == "self_reported"  # filled in for us

    code, listed, _ = cli(capsys, "list", "efforts")
    assert listed["count"] == 1 and listed["items"][0]["metrics"] == {"duration": {"value": 60, "unit": "s"}}
    assert listed["items"][0]["id"] == event["record"]["id"]
    assert cli(capsys, "list", "efforts", "--activity", "x.row")[1]["count"] == 0
    assert cli(capsys, "list", "activities")[1]["items"][0]["default_metrics"] == ["duration"]

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
    rid = out["event"]["record"]["id"]
    block = out["event"]["payload"]["blocks"][0]
    assert block["id"] == f"{rid}.0" and [e["id"] for e in block["efforts"]] == [f"{rid}.0.0", f"{rid}.0.1"]
    planned = cli(capsys, "list", "planned")[1]["items"][0]
    assert planned["blocks"][0]["rounds"] == 3
    assert planned["blocks"][0]["metrics"] == {"rest": {"value": 90, "unit": "s"}}
    assert [e["activity_id"] for e in planned["blocks"][0]["efforts"]] == ["x.pull_up", "x.dip"]
    assert planned["blocks"][0]["efforts"][0]["metrics"] == {"reps": {"min": 6, "max": 8}}



def test_editing_a_planned_sessions_blocks_keeps_their_ids(capsys):
    for id, name in (("x.pull_up", "Pull-Up"), ("x.dip", "Dip")):
        cli(capsys, "log", "activity", json.dumps({"name": name}), "--id", id)
    session = {"scheduled_on": "2026-11-23", "name": "Upper", "blocks": [
        {"rounds": 3, "efforts": [{"activity_id": "x.pull_up"}, {"activity_id": "x.dip"}]}]}
    rid = cli(capsys, "log", "planned_session", json.dumps(session))[1]["event"]["record"]["id"]
    swapped = {"blocks": [{"rounds": 4, "efforts": [{"activity_id": "x.dip"}, {"activity_id": "x.pull_up"}]}]}
    code, out, _ = cli(capsys, "edit", "planned_session", rid, json.dumps(swapped))
    assert code == 0 and out["event"]["payload"]["name"] == "Upper"
    block = out["event"]["payload"]["blocks"][0]
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
    session = cli(capsys, "list", "sessions")[1]["items"][0]
    assert session["metrics"]["duration"] == {"value": 90, "unit": "min"} and session["local_date"] == "2026-11-20"
    assert session["efforts"] == [out["events"][1]["record"]["id"]]


def test_log_from_file_and_stdin(capsys, tmp_path, monkeypatch):
    path = tmp_path / "ex.json"
    path.write_text(json.dumps({"name": "Plank"}))
    assert cli(capsys, "log", "activity", f"@{path}", "--id", "x.plank")[0] == 0
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({"name": "Row"})))
    assert cli(capsys, "log", "activity", "-", "--id", "x.row")[0] == 0
    assert cli(capsys, "list", "activities")[1]["count"] == 2


def test_actor_flag(capsys):
    code, out, _ = cli(capsys, "--actor", "agent:claude-code:example-model-1", "log", "activity",
                       json.dumps({"name": "Plank"}), "--id", "x.plank")
    assert out["event"]["actor"] == {"kind": "agent", "id": "claude-code", "model": "example-model-1"}


def test_actor_env(capsys, monkeypatch):
    monkeypatch.setenv("BYOBOT_ACTOR", "agent:example-coach")
    code, out, _ = cli(capsys, "log", "activity", json.dumps({"name": "Run"}), "--id", "x.run")
    assert out["event"]["actor"] == {"kind": "agent", "id": "example-coach"}
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
    rid = out["event"]["record"]["id"]
    code, edit, _ = cli(capsys, "edit", "effort", rid, '{"metrics": {"reps": {"value": 7}}, "notes": "last one didn\'t count"}')
    assert code == 0 and edit["event"]["prev"] == [out["event"]["id"]]
    effort = cli(capsys, "list", "efforts")[1]["items"][0]
    assert effort["metrics"] == {"reps": {"value": 7}}
    code, undo, _ = cli(capsys, "undo", edit["event"]["id"])
    assert code == 0 and undo["events"][0]["reverts"] == edit["event"]["id"]
    assert cli(capsys, "list", "efforts")[1]["items"][0]["metrics"] == {"reps": {"value": 8}}
    assert cli(capsys, "edit", "effort", "missing", '{"notes": "x"}')[0] != 0


def test_remove_and_history(capsys):
    cli(capsys, "log", "activity", json.dumps({"name": "Plank"}), "--id", "x.plank")
    assert cli(capsys, "remove", "activity", "x.plank", "--reason", "typo")[0] == 0
    assert cli(capsys, "list", "activities")[1]["count"] == 0
    versions = cli(capsys, "history", "activity", "x.plank")[1]["versions"]
    assert [v["op"] for v in versions] == ["put", "retract"]


def test_ingest_ndjson_file(capsys, tmp_path):
    path = tmp_path / "samples.ndjson"
    path.write_text("\n".join(json.dumps(s) for s in (WEIGHT, RESTING)) + "\n")
    code, out, _ = cli(capsys, "ingest", f"@{path}")
    assert out == {"written": {"hume": 1, "apple_health": 1}, "unchanged": 0}
    assert cli(capsys, "ingest", f"@{path}")[1] == {"written": {"hume": 0, "apple_health": 0}, "unchanged": 2}
    code, listed, _ = cli(capsys, "list", "observations", "--type", "body_mass")
    assert listed["count"] == 1 and listed["items"][0]["value"] == 82.4


def test_correct_and_exclude_an_import(capsys):
    cli(capsys, "ingest", json.dumps(WEIGHT))
    rid = cli(capsys, "list", "sources")[1]["items"][0]["id"]
    code, out, _ = cli(capsys, "correct", rid, '{"value": 81.9, "unit": "kg"}')
    assert code == 0, out
    assert cli(capsys, "list", "observations")[1]["items"][0]["value"] == 81.9
    assert cli(capsys, "exclude", rid, "--notes", "wrong person")[0] == 0
    assert cli(capsys, "list", "observations")[1]["count"] == 0
    assert cli(capsys, "correct", "missing", '{"value": 1}')[0] != 0


def test_validate(capsys):
    code, out, _ = cli(capsys, "validate", "--as", "activity", json.dumps({"name": "Plank"}))
    assert code == 0 and out["valid"]
    code, out, _ = cli(capsys, "validate", "--as", "observation", json.dumps({"code": "body_mass"}))
    assert code == 1 and not out["valid"] and out["errors"]
    code, out, _ = cli(capsys, "validate", json.dumps({"id": "x"}))
    assert code == 1 and not out["valid"]


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


def test_log_and_list_locations(capsys):
    code, out, _ = cli(capsys, "log", "location", json.dumps({"name": "Home gym"}), "--id", "home")
    assert code == 0 and out["event"]["record"] == {"kind": "location", "id": "home"}
    assert [i["name"] for i in cli(capsys, "list", "locations")[1]["items"]] == ["Home gym"]


def test_list_activities_by_source(capsys, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "activities").mkdir(parents=True)
    (lib / "vocabulary.json").write_text("{}")
    (lib / "activities" / "dip.json").write_text(json.dumps({"id": "x.dip", "name": "Dip", "targets": {"main": ["triceps"]}}))
    monkeypatch.setenv("BYOBOT_LIBRARY", str(lib))
    cli(capsys, "log", "activity", '{"name": "Plank"}', "--id", "x.plank")
    items = cli(capsys, "list", "activities")[1]["items"]
    assert [(i["id"], i["source"]) for i in items] == [("x.dip", "library"), ("x.plank", "store")]
    assert items[0]["targets"] == {"main": ["triceps"]} and "payload" not in items[0]
    assert [i["id"] for i in cli(capsys, "list", "activities", "--source", "library")[1]["items"]] == ["x.dip"]


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
        main(["import"])
    assert exit.value.code == 0 and "activities" in capsys.readouterr().out


def test_your_own_activity_ids_start_with_x(capsys):
    code, _, err = cli(capsys, "log", "activity", json.dumps({"name": "Plank"}), "--id", "plank")
    assert code == 1 and "x.plank" in json.loads(err)["message"]
    assert cli(capsys, "log", "activity", json.dumps({"name": "Plank"}), "--id", "x.plank")[0] == 0
