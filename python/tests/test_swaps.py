"""Swaps and last time at the gym: the app's rules, for agents too."""

import json
from pathlib import Path

import pytest

from byobot import swaps
from byobot.commands import Context, run
from byobot.util import NotFound

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "schema" / "fixtures" / "v1" / "swaps.json").read_text())
ME = {"kind": "human", "id": "me"}


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_swaps_match_the_shared_fixtures(case):
    library = [{"id": a["id"], **a["payload"]} for a in FIXTURE["library"]]
    original = next(a for a in library if a["id"] == case["activity_id"])
    have = None if case["equipment"] is None else set(case["equipment"])
    found = swaps.alternatives(original, library, have, case.get("limit", 8))
    assert [{"id": a["activity"]["id"], "score": a["score"], "missing_equipment": a["missing_equipment"]}
            for a in found] == case["expect"]


@pytest.fixture
def ctx(byobot_home, tmp_path, monkeypatch):
    lib = tmp_path / "swap-library"
    (lib / "activities").mkdir(parents=True)
    (lib / "vocabulary.json").write_text("{}")
    for a in FIXTURE["library"]:
        (lib / "activities" / f"{a['id']}.json").write_text(json.dumps({"id": a["id"], **a["payload"]}))
    monkeypatch.setenv("BYOBOT_LIBRARY", str(lib))
    c = Context(actor=ME)
    yield c
    c.close()


def log(ctx, kind, payload, id=None):
    return run("log", ctx, {"kind": kind, "payload": payload, "id": id})


def test_swap_list_uses_the_equipment_where_you_train(ctx):
    log(ctx, "location", {"name": "Example gym", "equipment": ["cable"]}, "example_gym")
    log(ctx, "location", {"name": "Home"}, "home")
    at_gym = run("swap list", ctx, {"activity": "pull_up", "location": "example_gym"})
    assert at_gym["equipment"] == ["cable"] and at_gym["items"][0]["id"] == "lat_pulldown"
    assert at_gym["items"][0]["missing_equipment"] == []
    assert run("swap list", ctx, {"activity": "pull_up", "location": "home"})["equipment"] is None
    log(ctx, "setting", {"value": ["dumbbell"]}, "equipment")
    assert run("swap list", ctx, {"activity": "pull_up", "location": "home"})["equipment"] == ["dumbbell"]
    assert len(run("swap list", ctx, {"activity": "pull_up", "limit": 2})["items"]) == 2
    with pytest.raises(NotFound):
        run("swap list", ctx, {"activity": "x.nothing"})
    with pytest.raises(NotFound):
        run("swap list", ctx, {"activity": "pull_up", "location": "nowhere"})


def squat(ctx, start, location=None, session=None, load=100):
    payload = {"activity_id": "barbell_back_squat", "time": {"start": start},
               "metrics": {"reps": {"value": 5}, **({"load": {"value": load, "unit": "kg"}} if load else {})}}
    if location:
        payload["location_id"] = location
    if session:
        payload["links"] = {"part_of": {"kind": "session", "id": session}}
    return log(ctx, "effort", payload)


def test_effort_history_compares_loads_only_at_one_location(ctx):
    log(ctx, "location", {"name": "Example gym"}, "example_gym")
    log(ctx, "location", {"name": "Home"}, "home")
    session = log(ctx, "session", {"location_id": "example_gym", "time": {"start": "2026-11-18T18:00:00Z"}})
    sid = session["event"]["record"]["id"]
    squat(ctx, "2026-11-18T18:00:00Z", session=sid)
    squat(ctx, "2026-11-18T18:05:00Z", session=sid, load=105)
    squat(ctx, "2026-11-20T18:00:00Z", location="home", load=60)

    gym = run("effort history", ctx, {"activity": "barbell_back_squat", "location": "example_gym"})
    assert gym["elsewhere"] is False and len(gym["times"]) == 1
    [time] = gym["times"]
    assert (time["session_id"], time["location_id"], time["date"]) == (sid, "example_gym", "2026-11-18")
    assert [s["metrics"]["load"]["value"] for s in time["sets"]] == [100, 105]

    anywhere = run("effort history", ctx, {"activity": "barbell_back_squat"})
    assert [t["location_id"] for t in anywhere["times"]] == ["home", "example_gym"]
    assert len(run("effort history", ctx, {"activity": "barbell_back_squat", "limit": 1})["times"]) == 1

    new_gym = run("effort history", ctx, {"activity": "barbell_back_squat", "location": "other_gym"})
    assert new_gym["elsewhere"] is True and new_gym["times"][0]["location_id"] == "home"
    with pytest.raises(NotFound):
        run("effort history", ctx, {"activity": "x.nothing"})


def test_effort_history_without_loads_ignores_the_location(ctx):
    squat(ctx, "2026-11-20T18:00:00Z", location="home", load=None)
    out = run("effort history", ctx, {"activity": "barbell_back_squat", "location": "example_gym"})
    assert out["elsewhere"] is False and out["times"][0]["location_id"] == "home"
