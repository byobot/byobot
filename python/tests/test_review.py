import json

from test_cli import cli


def log(capsys, type, payload):
    code, out, err = cli(capsys, "log", type, json.dumps(payload))
    assert code == 0, err
    return out


def test_review_summarizes_recent_training(capsys):
    log(capsys, "activity.defined", {"id": "barbell_row", "name": "Barbell Row",
                                     "targets": {"main": ["lats"], "synergists": ["biceps"]}})
    log(capsys, "location.defined", {"id": "example-gym", "name": "Example gym"})
    log(capsys, "session.planned", {"id": "mon", "scheduled_on": "2026-11-16", "name": "Pull",
                                    "location_id": "example-gym",
                                    "blocks": [{"id": "b1", "efforts": [{"id": "pe1", "activity_id": "barbell_row"}]}]})
    log(capsys, "session.planned", {"id": "wed", "scheduled_on": "2026-11-18", "name": "Pull again",
                                    "blocks": [{"id": "b2", "efforts": [{"id": "pe2", "activity_id": "barbell_row"}]}]})
    log(capsys, "session.planned", {"id": "next", "scheduled_on": "2026-11-25", "name": "Next",
                                    "blocks": [{"id": "b3", "efforts": [{"id": "pe3", "activity_id": "barbell_row"}]}]})
    log(capsys, "session.performed", {"id": "s1", "planned_session_id": "mon", "name": "Pull",
                                      "started_at": "2026-11-16T12:00:00Z", "location_id": "example-gym", "efforts": [
        {"activity_id": "barbell_row", "planned_effort_id": "pe1", "round": r, "performed_at": f"2026-11-16T12:0{r}:00Z",
         "metrics": {"reps": {"value": 8}, "load": {"value": 60, "unit": "kg"}}} for r in (1, 2)]})

    code, out, _ = cli(capsys, "review", "--weeks", "2", "--until", "2026-11-23")
    assert code == 0
    assert out["from"] == "2026-11-09" and out["to"] == "2026-11-23"
    assert out["sessions"] == [{"date": "2026-11-16", "name": "Pull", "location_id": "example-gym",
                                "planned_session_id": "mon", "sets": 2}]
    assert out["plan_adherence"] == {"planned": 2, "done": 1,
                                     "missed": [{"id": "wed", "scheduled_on": "2026-11-18", "name": "Pull again"}]}
    assert out["exercises"] == [{"activity_id": "barbell_row", "location_id": "example-gym", "days_done": 1,
                                 "recent": [{"date": "2026-11-16", "sets": [{"load": "60 kg", "reps": 8}] * 2}]}]
    assert out["weekly_sets_per_muscle"] == {"2026-11-16": {"biceps": {"main": 0, "synergist": 2},
                                                            "lats": {"main": 2, "synergist": 0}}}
    assert [p["id"] for p in out["upcoming"]] == ["next"]


def test_review_rejects_a_bad_date(capsys):
    assert cli(capsys, "review", "--until", "tomorrow")[0] != 0
