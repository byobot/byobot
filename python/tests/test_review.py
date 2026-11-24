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


def test_review_shows_recovery_and_imported_workouts(store):
    from byobot.review import review

    def sample(type_, start, end, value, unit=None):
        s = {"source": "whoop", "source_id": f"{type_}:{start}", "type": type_, "start": start, "end": end,
             "value": value}
        return {**s, "unit": unit} if unit else s

    store.ingest([
        sample("byobot.SleepDeepDuration", "2026-11-20T03:00:00Z", "2026-11-20T11:00:00Z", 5400, "s"),
        sample("byobot.SleepREMDuration", "2026-11-20T03:00:00Z", "2026-11-20T11:00:00Z", 7200, "s"),
        sample("byobot.RecoveryScore", "2026-11-20T12:00:00Z", "2026-11-20T12:00:00Z", 68, "%"),
        sample("byobot.HeartRateVariabilityRMSSD", "2026-11-20T12:00:00Z", "2026-11-20T12:00:00Z", 61.24, "ms"),
        sample("byobot.Strain", "2026-11-20T13:00:00Z", "2026-11-20T13:00:00Z", 12.7),
    ])
    store.append("session.performed", {"id": "whoop:workout:2026-11-20T17:00:00Z", "name": "running",
                                       "started_at": "2026-11-20T17:00:00Z",
                                       "metrics": {"duration": {"value": 2700, "unit": "s"}}},
                 {"kind": "connector", "id": "whoop"})
    out = review(store, 1, "2026-11-22")
    assert out["recovery"] == [{"date": "2026-11-20", "asleep_h": 3.5, "hrv_rmssd_ms": 61.2, "recovery_pct": 68,
                                "strain": 12.7}]
    assert out["sessions"][0]["metrics"] == {"duration": "45 min"}
    assert out["sessions"][0]["imported_by"] == "whoop"


def test_review_shows_durations_readably():
    from byobot.review import readable_seconds

    assert [readable_seconds(s) for s in (45, 90, 120, 2295.5, 3600, 4320)] == [
        "45 s", "1 min 30 s", "2 min", "38 min", "1 h", "1 h 12 min"]
