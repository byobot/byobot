from datetime import datetime

ME = {"kind": "human", "id": "me"}


def at(text):  # local time, so dates hold in any time zone
    return datetime.fromisoformat(text).astimezone().isoformat()


def lift(store):
    store.append("activity.defined", {"id": "squat", "name": "Squat",
                                      "targets": {"main": ["quads", "glutes"], "synergists": ["adductors"]}}, ME)
    store.append("activity.defined", {"id": "plank", "name": "Plank", "targets": {"main": ["abs"]}}, ME)
    store.append("session.performed", {
        "id": "s1", "started_at": at("2026-11-16T18:00:00"), "ended_at": at("2026-11-16T19:00:00"),
        "location_id": "example-gym", "efforts": [
            {"id": "e1", "activity_id": "squat", "performed_at": at("2026-11-16T18:05:00"),
             "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}},
            {"id": "e2", "activity_id": "squat", "performed_at": at("2026-11-16T18:10:00"),
             "metrics": {"reps": {"value": 3}, "load": {"value": 225, "unit": "lb"}}},
            {"id": "e3", "activity_id": "plank", "performed_at": at("2026-11-16T18:20:00"),
             "metrics": {"duration": {"value": 60, "unit": "s"}}},
        ]}, ME)


def test_daily_has_one_row_per_day_with_training(store):
    lift(store)
    store.ingest([{"source": "whoop", "source_id": "r1", "type": "byobot.RecoveryScore",
                   "start": at("2026-11-17T07:00:00"), "end": at("2026-11-17T07:00:00"), "value": 71, "unit": "%"}])
    rows = store.rows("SELECT date, recovery_pct, training_sessions, training_min, training_sets,"
                      " round(training_volume_kg, 1) AS volume FROM daily ORDER BY date")
    assert rows == [
        {"date": "2026-11-16", "recovery_pct": None, "training_sessions": 1, "training_min": 60.0, "training_sets": 3,
         "volume": 806.2},
        {"date": "2026-11-17", "recovery_pct": 71, "training_sessions": 0, "training_min": None, "training_sets": 0,
         "volume": 0},
    ]


def test_views_reach_stores_made_before_them(byobot_home):
    from byobot.store import Store

    path = byobot_home / "old.db"
    with Store(path) as s:
        s.conn.execute("DROP VIEW daily")
        s.conn.execute("UPDATE meta SET value = 'old' WHERE key = 'views'")
    with Store(path) as s:
        assert s.rows("SELECT count(*) AS n FROM daily") == [{"n": 0}]
