import json

from test_cli import cli


def define(capsys, activity):
    rid = activity.pop("id")
    assert cli(capsys, "log", "activity", json.dumps(activity), "--id", rid)[0] == 0


def test_list_activities_by_muscle_and_equipment(capsys):
    for a in ({"id": "x.cable_row", "name": "Cable Row", "equipment": ["cable"],
               "targets": {"main": ["lats"], "synergists": ["biceps"]}},
              {"id": "x.cable_curl", "name": "Cable Curl", "equipment": ["cable"], "targets": {"main": ["biceps"]}},
              {"id": "x.dumbbell_row", "name": "Dumbbell Row", "equipment": ["dumbbell"], "targets": {"main": ["lats"]}},
              {"id": "x.plank", "name": "Plank"}):
        define(capsys, a)

    def ids(*argv):
        return [i["id"] for i in cli(capsys, "list", "activities", *argv)[1]["items"]]

    assert ids("--muscle", "biceps") == ["x.cable_curl", "x.cable_row"]
    assert ids("--muscle", "lats", "--equipment", "cable") == ["x.cable_row"]
    assert ids("--source", "store", "--muscle", "lats", "--equipment", "dumbbell") == ["x.dumbbell_row"]


def test_list_activities_brief(capsys):
    define(capsys, {"id": "x.cable_row", "name": "Cable Row", "equipment": ["cable"], "targets": {"main": ["lats"]},
                    "notes": "Neutral grip."})
    define(capsys, {"id": "x.plank", "name": "Plank"})
    brief = cli(capsys, "list", "activities", "--detail", "brief")[1]["items"]
    assert brief == [{"id": "x.cable_row", "name": "Cable Row", "targets": {"main": ["lats"]}, "equipment": ["cable"],
                      "source": "store"}, {"id": "x.plank", "name": "Plank", "source": "store"}]


def test_schema_vocabulary(capsys, tmp_path, monkeypatch):
    (tmp_path / "library" / "vocabulary.json").write_text(json.dumps({"muscles": ["lats"], "equipment": ["cable"]}))
    assert cli(capsys, "schema", "vocabulary")[1] == {"name": "vocabulary", "muscles": ["lats"], "equipment": ["cable"]}
