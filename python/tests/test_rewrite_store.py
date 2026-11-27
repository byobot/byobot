"""tools/rewrite_store.py on a hand-made store from before ids and codes were settled."""

import copy
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from byobot.core import commands, hlc, sources
from byobot.core.store import Store
from byobot.util import ByobotError, uuid7

ME = {"kind": "human", "id": "me"}
TOOL = Path(__file__).resolve().parents[2] / "tools" / "rewrite_store.py"
spec = importlib.util.spec_from_file_location("rewrite_store", TOOL)
rewrite_store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rewrite_store)


class Wall:
    def __init__(self):
        self.at = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


def old_store(path: Path) -> dict:
    """A store as byobot wrote it before: dashed ids, unprefixed activities, a load in %{1RM}, WHOOP's bare codes,
    equipment as one list and a rearrange overlay. Written raw, since this byobot refuses all of them."""
    ids = {}
    with Store(path, device_id="example-laptop", wall=Wall()) as s:
        sources.ingest_samples(s, [{"source": "apple_health", "source_id": "a1", "type": "body_mass",
                                    "start": "2026-03-01T07:00:00Z", "value": 80, "unit": "kg"}])
        [template] = commands.log(s, ME, [{"kind": "session", "payload": {}}])
        clock = hlc.parse(template["hlc"])

        def put(kind, rid, payload, op="put", prev=()):
            nonlocal clock
            clock = (clock[0], clock[1] + 1)
            e = copy.deepcopy(template) | {"id": uuid7(), "record": {"kind": kind, "id": rid}, "op": op,
                                           "hlc": hlc.format(clock), "prev": list(prev), "payload": payload}
            with s._tx():
                s._insert(e)
            return e["id"]

        put("location", "example-gym", {"name": "Gym"})
        put("setting", "current_location", {"value": "example-gym"})
        put("setting", "equipment", {"value": ["barbell", "plates", "ez_barbell", "bench"]})
        put("setting", "source_priority", {"value": {"default": ["whoop"], "codes": {"strain": ["whoop"]}}})
        put("activity", "cable-row", {"name": "Cable Row", "equipment": ["cable", "bench"], "tags": ["pull-day"]})
        put("activity", "face_pulls", {"name": "Face Pull", "equipment": ["cable"]})
        put("activity", "smith-squat", {"name": "Smith Squat", "equipment": ["smith_machine"]})
        put("activity", "landmine.press", {"name": "Landmine Press", "equipment": ["plates", "barbell"]})
        put("activity", "hack-squat", {"name": "Hack Squat", "equipment": ["plates"]})
        ids["session"] = template["record"]["id"]
        at = {"time": {"start": "2026-03-02T07:00:00-05:00"}, "modality": "self_reported"}
        part_of = {"part_of": {"kind": "session", "id": ids["session"]}}
        put("effort", ids.setdefault("pct", uuid7()), {**at, "activity_id": "cable-row", "location_id": "example-gym",
                                                         "metrics": {"reps": {"value": 8},
                                                                     "load": {"value": 75, "unit": "%{1RM}"}},
                                                         "links": part_of})
        put("effort", ids.setdefault("kg", uuid7()), {**at, "activity_id": "single_arm_dumbbell_row",
                                                        "metrics": {"load": {"value": 20, "unit": "kg"}}})
        put("observation", ids.setdefault("strain", uuid7()), {**at, "code": "strain", "value": 12})
        ids["planned"] = uuid7()
        first = put("planned_session", ids["planned"], {"scheduled_on": "2026-03-03", "name": "Pull", "blocks": [
            {"id": f"{ids['planned']}.0", "efforts": [{"id": f"{ids['planned']}.0.0", "activity_id": "ez_barbell_curl",
                                                      "metrics": {"load": {"value": 60, "unit": "%{1RM}"}}}]}]})
        rearrange = s.contract.derived("overlay", ["rearrange", "planned_session", ids["planned"]])
        last = put("overlay", rearrange, {"effect": "rearrange", "target": {"kind": "planned_session",
                                                                           "id": ids["planned"]},
                                          "order": [f"{ids['planned']}.0.0"]})
        put("overlay", rearrange, {}, op="retract", prev=[last])
        ids["first"] = first
        s.conn.execute("PRAGMA user_version = 5")
    return ids


def test_an_old_store_is_refused_until_rewritten(tmp_path):
    old_store(tmp_path / "byobot.db")
    with pytest.raises(ByobotError, match="rewrite_store"):
        Store(tmp_path / "byobot.db")


def test_the_rewrite_settles_ids_and_codes_and_keeps_the_rest(tmp_path, capsys):
    path = tmp_path / "byobot.db"
    ids = old_store(path)
    old = sqlite3.connect(path).execute("SELECT value FROM meta WHERE key IN ('store_id', 'store_key') "
                                        "ORDER BY key").fetchall()
    assert rewrite_store.main([str(path)]) == 0
    assert (tmp_path / "byobot.db.before_rewrite").is_file()
    with Store(path) as s:
        def get(kind, rid):
            return s.get(kind, rid)["payload"]

        assert s.store_id != old[0][0] and s._get("store_key") == old[1][0]
        assert s._get("device_id") == "example-laptop"
        assert get("location", "example_gym")["name"] == "Gym"
        assert get("setting", "current_location")["value"] == "example_gym"
        assert get("setting", "equipment")["value"] == ["barbell", "plate_loaded_machine", "ez_bar", "bench"]
        assert get("setting", "source_priority")["value"]["codes"] == {"whoop_strain": ["whoop"]}
        row = get("activity", "x.cable_row")
        assert (row["load_type"], row["equipment"], row["tags"]) == ("cable", ["bench"], ["pull_day"])
        assert get("activity", "face_pull")["load_type"] == "cable"
        smith = get("activity", "x.smith_squat")
        assert (smith["load_type"], smith["equipment"]) == ("plate_loaded_machine", ["smith_machine"])
        landmine = get("activity", "x.landmine_press")
        assert (landmine["load_type"], "equipment" in landmine) == ("barbell", False)
        assert get("activity", "x.hack_squat")["load_type"] == "plate_loaded_machine"
        pct = get("effort", ids["pct"])
        assert (pct["activity_id"], pct["location_id"]) == ("x.cable_row", "example_gym")
        assert pct["metrics"] == {"reps": {"value": 8}, "pct_of_1rm": {"value": 75}}
        assert get("effort", ids["kg"])["activity_id"] == "dumbbell_one_arm_row"
        assert get("observation", ids["strain"])["code"] == "whoop_strain"
        planned = get("planned_session", ids["planned"])["blocks"][0]["efforts"][0]
        assert planned["activity_id"] == "ez_bar_curl" and planned["metrics"] == {"pct_of_1rm": {"value": 60}}
        assert not s.conn.execute("SELECT 1 FROM events WHERE kind = 'overlay'").fetchone()
        assert s.event(ids["first"])["hlc"]  # events keep their ids
        assert s.conn.execute("SELECT count(*) FROM source_records").fetchone()[0] == 1
        again = tmp_path / "again.db"
        with Store(again) as copy_:
            copy_.restore(s.export())  # what it holds passes every check again
    with pytest.raises(SystemExit, match="in the way"):
        rewrite_store.main([str(path)])


def test_two_ids_that_become_one_stop_the_rewrite(tmp_path):
    path = tmp_path / "byobot.db"
    old_store(path)
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT * FROM events WHERE kind = 'location'").fetchone()
    conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (None, uuid7(), "location", "example.gym", *row[4:]))
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit, match="both become location example_gym"):
        rewrite_store.main([str(path)])
    assert path.is_file() and not (tmp_path / "byobot.db.before_rewrite").exists()
