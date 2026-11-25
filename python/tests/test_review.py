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


def test_review_takes_each_metric_from_one_source(store):
    from datetime import datetime

    from byobot.review import review

    def at(text):  # local time, so dates hold in any time zone
        return datetime.fromisoformat(text).astimezone().isoformat()

    def sample(source, type_, start, end, value, unit=None, device=None):
        s = {"source": source, "source_id": f"{type_}:{start}:{value}", "type": type_, "start": at(start), "end": at(end),
             "value": value}
        if device:
            s["device"] = {"id": device}
        return {**s, "unit": unit} if unit else s

    def segment(start, end, stage, device):
        return sample("apple-health", "HKCategoryTypeIdentifierSleepAnalysis", start, end, stage, device=device)

    store.ingest([
        # Nov 20: WHOOP directly, and its copies in Apple Health.
        sample("whoop", "byobot.SleepDeepDuration", "2026-11-20T03:00:00", "2026-11-20T11:00:00", 7200, "s"),
        sample("whoop", "byobot.HeartRateVariabilityRMSSD", "2026-11-20T12:00:00", "2026-11-20T12:00:00", 61, "ms"),
        sample("whoop", "HKQuantityTypeIdentifierRestingHeartRate", "2026-11-20T12:00:00", "2026-11-20T12:00:00",
               48, "count/min"),
        segment("2026-11-20T03:00:00", "2026-11-20T11:00:00", "asleepUnspecified", "example-strap"),
        sample("apple-health", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "2026-11-20T12:00:00",
               "2026-11-20T12:00:00", 61, "ms"),
        sample("apple-health", "HKQuantityTypeIdentifierRestingHeartRate", "2026-11-20T12:00:00",
               "2026-11-20T12:00:00", 48, "count/min"),
        sample("apple-health", "HKQuantityTypeIdentifierBodyMass", "2026-11-20T12:10:00", "2026-11-20T12:10:00",
               82.4, "kg"),
        sample("apple-health", "HKQuantityTypeIdentifierBodyFatPercentage", "2026-11-20T12:10:00",
               "2026-11-20T12:10:00", 18.4, "%"),
        # Nov 21: Apple Health only, with two apps recording the night.
        segment("2026-11-21T03:00:00", "2026-11-21T05:00:00", "asleepCore", "example-watch"),
        segment("2026-11-21T05:00:00", "2026-11-21T06:00:00", "awake", "example-watch"),
        segment("2026-11-21T06:00:00", "2026-11-21T09:30:00", "asleepREM", "example-watch"),
        segment("2026-11-21T03:00:00", "2026-11-21T09:00:00", "asleepUnspecified", "example-ring"),
        segment("2026-11-21T02:30:00", "2026-11-21T09:30:00", "inBed", "example-watch"),
        sample("apple-health", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "2026-11-21T12:00:00",
               "2026-11-21T12:00:00", 55, "ms"),
    ])
    out = review(store, 1, "2026-11-22")
    assert out["recovery"] == [
        {"date": "2026-11-20", "asleep_h": 2.0, "hrv_rmssd_ms": 61, "resting_hr_bpm": 48},
        {"date": "2026-11-21", "asleep_h": 6.0, "hrv_sdnn_ms": 55},
    ]
    assert out["body"] == [{"date": "2026-11-20", "body_fat_pct": 18.4, "weight_kg": 82.4}]


def test_review_shows_activity_mobility_and_vitals(store):
    from datetime import datetime

    from byobot.review import review

    def at(text):
        return datetime.fromisoformat(text).astimezone().isoformat()

    def sample(source, type_, start, end, value, unit):
        return {"source": source, "source_id": f"{type_}:{start}", "type": f"HKQuantityTypeIdentifier{type_}",
                "start": at(start), "end": at(end), "value": value, "unit": unit}

    day = ("2026-11-20T00:00:00", "2026-11-21T00:00:00")
    store.ingest([
        sample("apple-health", "StepCount", *day, 9412, "count"),
        sample("apple-health", "DistanceWalkingRunning", *day, 7210.5, "m"),
        sample("apple-health", "AppleExerciseTime", *day, 2520, "s"),
        sample("apple-health", "HeartRate", *day, 71.4, "count/min"),
        sample("apple-health", "WalkingSpeed", "2026-11-20T09:00:00", "2026-11-20T09:05:00", 1.4, "m/s"),
        sample("apple-health", "WalkingSpeed", "2026-11-20T17:00:00", "2026-11-20T17:05:00", 1.2, "m/s"),
        sample("apple-health", "BloodPressureSystolic", "2026-11-20T08:00:00", "2026-11-20T08:00:00", 120, "mmHg"),
        sample("apple-health", "BloodPressureSystolic", "2026-11-20T20:00:00", "2026-11-20T20:00:00", 116, "mmHg"),
        sample("apple-health", "WaistCircumference", "2026-11-20T08:00:00", "2026-11-20T08:00:00", 0.82, "m"),
        # A night's wrist temperature counts toward the morning, and a direct source's skin temperature wins.
        sample("apple-health", "AppleSleepingWristTemperature", "2026-11-20T23:30:00", "2026-11-21T06:30:00",
               34.9, "degC"),
        sample("apple-health", "AppleSleepingWristTemperature", "2026-11-21T23:30:00", "2026-11-22T06:30:00",
               35.1, "degC"),
        {"source": "whoop", "source_id": "skin", "type": "byobot.SkinTemperature", "start": at("2026-11-21T07:00:00"),
         "end": at("2026-11-21T07:00:00"), "value": 33.4, "unit": "degC"},
    ])
    out = review(store, 1, "2026-11-22")
    assert out["activity"] == [{"date": "2026-11-20", "avg_hr_bpm": 71.4, "exercise_min": 42, "steps": 9412,
                                "walk_run_km": 7.2}]
    assert out["mobility"] == [{"date": "2026-11-20", "walking_speed_kmh": 4.7}]
    assert out["vitals"] == [{"date": "2026-11-20", "bp_systolic_mmhg": 118}]
    assert out["body"] == [{"date": "2026-11-20", "waist_cm": 82}]
    assert [(d["date"], d["skin_temp_c"]) for d in out["recovery"]] == [("2026-11-21", 33.4), ("2026-11-22", 35.1)]
