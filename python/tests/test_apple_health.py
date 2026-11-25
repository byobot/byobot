import json

import pytest
from test_whoop import YOU, import_whoop, log_session, whoop_api, workout

from byobot import apple_health
from byobot.serve import accept_samples


def health_workout(uid="5b0f2c1e-0000-4000-8000-000000000001", start="2026-11-20T17:00:00Z",
                   end="2026-11-20T17:45:00Z", activity="running", device="example-watch", **totals):
    return {"source": "apple-health", "source_id": uid, "type": "HKWorkoutTypeIdentifier", "start": start,
            "end": end, "value": activity, "device": {"id": device},
            "raw": {"duration_s": 2700, "energy_kcal": 480.25, "heart_rate_avg_bpm": 141, **totals}}


def sessions(store):
    return [r["id"] for r in store.rows("SELECT id FROM performed_sessions ORDER BY id")]


def test_a_workout_becomes_a_session(store):
    assert accept_samples(store, [health_workout(distance_m=7000)])["inserted"] == 1
    session = store.rows("SELECT * FROM performed_sessions")[0]
    assert session["id"] == "apple-health:workout:5b0f2c1e-0000-4000-8000-000000000001"
    assert (session["name"], session["actor_kind"], session["actor_id"]) == ("running", "connector", "apple-health")
    metrics = store.metrics_of("performed_session", session["id"])
    assert metrics["duration"]["value"] == 2700 and metrics["heart_rate"]["value"] == 141
    assert metrics["x.calories"] == {"value": 480.2, "unit": "kcal"} and metrics["distance"]["value"] == 7000
    assert metrics["x.healthkit_activity"] == {"value": "running"}
    events = store.counts()["events"]
    accept_samples(store, [health_workout(distance_m=7000)])
    assert store.counts()["events"] == events


@pytest.mark.parametrize("code, name", [("traditionalStrengthTraining", "strength training"),
                                        ("crossCountrySkiing", "cross country skiing"), ("yoga", "yoga")])
def test_activities_get_readable_names(code, name):
    assert apple_health.activity(code) == name


def test_two_apps_writing_one_workout_make_one_session(store):
    accept_samples(store, [health_workout(),
                           health_workout("5b0f2c1e-0000-4000-8000-000000000002", start="2026-11-20T17:02:00Z",
                                          end="2026-11-20T17:40:00Z", device="example-phone", duration_s=2280)])
    assert sessions(store) == ["apple-health:workout:5b0f2c1e-0000-4000-8000-000000000001"]


def test_a_workout_over_a_session_you_logged_adds_its_metrics_to_yours(store):
    log_session(store, metrics={"duration": {"value": 3000, "unit": "s"}})
    accept_samples(store, [health_workout()])
    assert sessions(store) == ["s1"]
    metrics = store.metrics_of("performed_session", "s1")
    assert metrics["duration"]["value"] == 3000 and metrics["heart_rate"]["value"] == 141


def test_whoop_imported_first_keeps_the_workout(monkeypatch, store):
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    accept_samples(store, [health_workout()])
    assert sessions(store) == ["whoop:workout:00000000-0000-4000-8000-000000000003"]


def test_whoop_imported_later_replaces_the_apple_health_session(monkeypatch, store):
    accept_samples(store, [health_workout()])
    out, _ = import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    assert out["sessions"]["added"] == 1
    assert sessions(store) == ["whoop:workout:00000000-0000-4000-8000-000000000003"]
    accept_samples(store, [health_workout()])
    assert sessions(store) == ["whoop:workout:00000000-0000-4000-8000-000000000003"]


def test_whoop_metrics_replace_apple_healths_on_a_session_you_logged(monkeypatch, store):
    log_session(store)
    accept_samples(store, [health_workout()])
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    accept_samples(store, [health_workout()])
    assert sessions(store) == ["s1"]
    metrics = store.metrics_of("performed_session", "s1")
    assert metrics["heart_rate"]["value"] == 148 and "x.healthkit_activity" not in metrics
    assert store.rows("SELECT id FROM performed_efforts WHERE session_id = 's1'") == [{"id": "e1"}]


def test_your_undo_of_an_apple_health_session_stands(store):
    accept_samples(store, [health_workout()])
    event = store.rows("SELECT id FROM events WHERE type = 'session.performed'")[0]["id"]
    store.append("event.undone", {"event_id": event}, YOU)
    accept_samples(store, [health_workout()])
    assert sessions(store) == []


def test_review_shows_where_a_session_came_from(store):
    from byobot.review import review

    accept_samples(store, [health_workout()])
    reviewed = review(store, 1, "2026-11-22")["sessions"]
    assert reviewed[0]["imported_by"] == "apple-health" and reviewed[0]["metrics"]["duration"] == "45 min"
    assert json.loads(store.rows("SELECT payload FROM events")[0]["payload"])["name"] == "running"
