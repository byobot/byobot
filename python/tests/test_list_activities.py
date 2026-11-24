import json

from test_cli import cli


def test_list_activities_by_muscle_and_equipment(capsys):
    for a in ({"id": "cable_row", "name": "Cable Row", "equipment": ["cable"],
               "targets": {"main": ["lats"], "synergists": ["biceps"]}},
              {"id": "cable_curl", "name": "Cable Curl", "equipment": ["cable"], "targets": {"main": ["biceps"]}},
              {"id": "dumbbell_row", "name": "Dumbbell Row", "equipment": ["dumbbell"], "targets": {"main": ["lats"]}},
              {"id": "plank", "name": "Plank"}):
        cli(capsys, "log", "activity.defined", json.dumps(a))

    def ids(*argv):
        return [i["id"] for i in cli(capsys, "list", "activities", *argv)[1]["items"]]

    assert ids("--muscle", "biceps") == ["cable_curl", "cable_row"]
    assert ids("--muscle", "lats", "--equipment", "cable") == ["cable_row"]
    assert ids("--source", "ledger", "--muscle", "lats", "--equipment", "dumbbell") == ["dumbbell_row"]


def test_list_activities_brief(capsys):
    cli(capsys, "log", "activity.defined", json.dumps({"id": "cable_row", "name": "Cable Row", "equipment": ["cable"],
                                                         "targets": {"main": ["lats"]}, "notes": "Neutral grip."}))
    cli(capsys, "log", "activity.defined", json.dumps({"id": "plank", "name": "Plank"}))
    brief = cli(capsys, "list", "activities", "--detail", "brief")[1]["items"]
    assert brief == [{"id": "cable_row", "name": "Cable Row", "targets": {"main": ["lats"]}, "equipment": ["cable"],
                      "source": "ledger"}, {"id": "plank", "name": "Plank", "source": "ledger"}]


def test_schema_vocabulary(capsys, tmp_path, monkeypatch):
    (tmp_path / "library" / "vocabulary.json").write_text(json.dumps({"muscles": ["lats"], "equipment": ["cable"]}))
    assert cli(capsys, "schema", "vocabulary")[1] == {"name": "vocabulary", "muscles": ["lats"], "equipment": ["cable"]}
