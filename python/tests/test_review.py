import json
from datetime import datetime

from test_cli import cli

from byobot.core import sources

MON, WED, NEXT, S1 = (f"01a2147e-0000-7000-8000-00000000000{i}" for i in range(1, 5))


def log(capsys, kind, payload, id=None):
    code, out, err = cli(capsys, "log", kind, json.dumps(payload), *(("--id", id) if id else ()))
    assert code == 0, err
    return out


def at(text):  # local time, so dates hold in any time zone
    return datetime.fromisoformat(text).astimezone().isoformat()


def test_review_summarizes_recent_training(capsys):
    log(capsys, "activity", {"name": "Barbell Row", "targets": {"main": ["lats"], "synergists": ["biceps"]}},
        "barbell_row")
    log(capsys, "location", {"name": "Example gym"}, "example_gym")
    for rid, day, name in ((MON, "2026-11-16", "Pull"), (WED, "2026-11-18", "Pull again"), (NEXT, "2026-11-25", "Next")):
        log(capsys, "planned_session", {"scheduled_on": day, "name": name, "location_id": "example_gym",
                                        "blocks": [{"efforts": [{"activity_id": "barbell_row"}]}]}, rid)
    sets = [{"kind": "effort", "payload": {
        "activity_id": "barbell_row", "round": r, "time": {"start": f"2026-11-16T12:0{r}:00Z"},
        "links": {"part_of": {"kind": "session", "id": S1},
                  "answers": {"kind": "planned_session", "id": MON, "part": f"{MON}.0.0"}},
        "metrics": {"reps": {"value": 8}, "load": {"value": 60, "unit": "kg"}}}} for r in (1, 2)]
    session = {"kind": "session", "id": S1, "payload": {
        "name": "Pull", "location_id": "example_gym", "links": {"answers": {"kind": "planned_session", "id": MON}},
        "time": {"start": "2026-11-16T12:00:00Z", "end": "2026-11-16T12:45:00Z"}}}
    log(capsys, "batch", [session, *sets])

    code, out, _ = cli(capsys, "review", "--weeks", "2", "--until", "2026-11-23")
    assert code == 0
    assert out["from"] == "2026-11-09" and out["to"] == "2026-11-23"
    assert out["sessions"] == [{"date": "2026-11-16", "name": "Pull", "location_id": "example_gym",
                                "planned_session_id": MON, "sets": 2}]
    assert out["plan_adherence"] == {"planned": 2, "done": 1,
                                     "missed": [{"id": WED, "scheduled_on": "2026-11-18", "name": "Pull again"}]}
    assert out["exercises"] == [{"activity_id": "barbell_row", "location_id": "example_gym", "days_done": 1,
                                 "recent": [{"date": "2026-11-16", "sets": [{"load": "60 kg", "reps": 8}] * 2}]}]
    assert out["weekly_sets_per_muscle"] == {"2026-11-16": {"biceps": {"main": 0, "synergist": 2},
                                                            "lats": {"main": 2, "synergist": 0}}}
    assert [p["id"] for p in out["upcoming"]] == [NEXT]

    assert cli(capsys, "edit", "planned_session", WED, '{"status": "skipped"}')[0] == 0
    assert cli(capsys, "review", "--weeks", "2", "--until", "2026-11-23")[1]["plan_adherence"]["missed"] == []


def test_review_rejects_a_bad_date(capsys):
    assert cli(capsys, "review", "--until", "tomorrow")[0] != 0


def whoop(store, *records):
    sources.ingest(store, "whoop", list(records))


def scored(raw, **score):
    return {"updated_at": "2026-11-20T13:00:00Z", "score_state": "SCORED", **raw, "score": score}


def test_review_shows_recovery_and_imported_workouts(store):
    from byobot.review import review

    whoop(store,
          ("sleep", "n1", scored({"id": "n1", "start": "2026-11-20T03:00:00Z", "end": "2026-11-20T11:00:00Z",
                                  "timezone_offset": "-05:00"},
                                 stage_summary={"total_slow_wave_sleep_time_milli": 5_400_000,
                                                "total_rem_sleep_time_milli": 7_200_000})),
          ("recovery", "7", scored({"cycle_id": 7, "sleep_id": "n1", "created_at": "2026-11-20T12:00:00Z"},
                                   recovery_score=68, hrv_rmssd_milli=61.24)),
          ("cycle", "7", scored({"id": 7, "start": "2026-11-20T11:00:00Z", "timezone_offset": "-05:00"}, strain=12.7)),
          ("workout", "w1", scored({"id": "w1", "start": "2026-11-20T17:00:00Z", "end": "2026-11-20T17:45:00Z",
                                    "timezone_offset": "-05:00", "sport_name": "running"})))
    out = review(store, 1, "2026-11-22")
    assert out["recovery"] == [{"date": "2026-11-20", "asleep_h": 3.5, "hrv_rmssd_ms": 61.2, "recovery_pct": 68,
                                "strain": 12.7}]
    assert out["sessions"] == [{"date": "2026-11-20", "name": "running", "metrics": {"duration": "45 min"},
                                "sources": ["whoop"]}]


def test_review_shows_durations_readably():
    from byobot.review import readable_seconds

    assert [readable_seconds(s) for s in (45, 90, 120, 2295.5, 3600, 4320)] == [
        "45 s", "1 min 30 s", "2 min", "38 min", "1 h", "1 h 12 min"]


def sample(source, type_, start, end, value, unit=None, device=None):
    s = {"source": source, "source_id": f"{type_}:{start}:{value}:{device}", "type": type_, "start": at(start),
         "end": at(end), "value": value}
    if device:
        s["device"] = device
    return {**s, "unit": unit} if unit else s


def test_review_takes_each_metric_from_one_source(store):
    from byobot.review import review

    def segment(start, end, stage, device):
        return sample("apple_health", "HKCategoryTypeIdentifierSleepAnalysis", start, end, stage, device=device)

    # Nov 20: WHOOP directly, and its copies in Apple Health.
    whoop(store,
          ("sleep", "n1", scored({"id": "n1", "start": at("2026-11-20T03:00:00"), "end": at("2026-11-20T11:00:00")},
                                 stage_summary={"total_slow_wave_sleep_time_milli": 7_200_000})),
          ("recovery", "7", scored({"cycle_id": 7, "sleep_id": "n1", "created_at": at("2026-11-20T12:00:00")},
                                   hrv_rmssd_milli=61, resting_heart_rate=48)))
    sources.ingest_samples(store, [
        segment("2026-11-20T03:00:00", "2026-11-20T11:00:00", "asleepUnspecified", "Example strap"),
        sample("apple_health", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "2026-11-20T12:00:00",
               "2026-11-20T12:00:00", 61, "ms"),
        sample("apple_health", "HKQuantityTypeIdentifierRestingHeartRate", "2026-11-20T12:00:00",
               "2026-11-20T12:00:00", 48, "count/min"),
        sample("apple_health", "HKQuantityTypeIdentifierBodyMass", "2026-11-20T12:10:00", "2026-11-20T12:10:00",
               82.4, "kg"),
        sample("apple_health", "HKQuantityTypeIdentifierBodyFatPercentage", "2026-11-20T12:10:00",
               "2026-11-20T12:10:00", 18.4, "%"),
        # Nov 21: Apple Health only, with two apps recording the night.
        segment("2026-11-21T03:00:00", "2026-11-21T05:00:00", "asleepCore", "Example Watch"),
        segment("2026-11-21T05:00:00", "2026-11-21T06:00:00", "awake", "Example Watch"),
        segment("2026-11-21T06:00:00", "2026-11-21T09:30:00", "asleepREM", "Example Watch"),
        segment("2026-11-21T03:00:00", "2026-11-21T09:00:00", "asleepUnspecified", "Example Ring"),
        segment("2026-11-21T02:30:00", "2026-11-21T09:30:00", "inBed", "Example Watch"),
        sample("apple_health", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "2026-11-21T12:00:00",
               "2026-11-21T12:00:00", 55, "ms"),
    ])
    out = review(store, 1, "2026-11-22")
    assert out["recovery"] == [
        {"date": "2026-11-20", "asleep_h": 2.0, "hrv_rmssd_ms": 61, "resting_hr_bpm": 48},
        {"date": "2026-11-21", "asleep_h": 6.0, "hrv_sdnn_ms": 55},
    ]
    assert out["body"] == [{"date": "2026-11-20", "body_fat_pct": 18.4, "weight_kg": 82.4}]


def test_your_own_measurement_comes_first(capsys, store):
    sources.ingest_samples(store, [sample("hume", "HKQuantityTypeIdentifierBodyMass", "2026-11-20T07:00:00",
                                          "2026-11-20T07:00:00", 82.4, "kg")])
    store.close()
    assert cli(capsys, "observe", "body_mass", "81.5", "--unit", "kg", "--start", "2026-11-20T08:00:00")[0] == 0
    assert cli(capsys, "review", "--weeks", "1", "--until", "2026-11-22")[1]["body"] == [
        {"date": "2026-11-20", "weight_kg": 81.5}]


def test_review_shows_activity_mobility_and_vitals(store):
    from byobot.review import review

    def quantity(type_, start, end, value, unit):
        return sample("apple_health", f"HKQuantityTypeIdentifier{type_}", start, end, value, unit)

    day = ("2026-11-20T00:00:00", "2026-11-21T00:00:00")
    sources.ingest_samples(store, [
        quantity("StepCount", *day, 9412, "count"),
        quantity("DistanceWalkingRunning", *day, 7210.5, "m"),
        quantity("AppleExerciseTime", *day, 2520, "s"),
        quantity("HeartRate", *day, 71.4, "count/min"),
        quantity("WalkingSpeed", "2026-11-20T09:00:00", "2026-11-20T09:05:00", 1.4, "m/s"),
        quantity("WalkingSpeed", "2026-11-20T17:00:00", "2026-11-20T17:05:00", 1.2, "m/s"),
        quantity("BloodPressureSystolic", "2026-11-20T08:00:00", "2026-11-20T08:00:00", 120, "mmHg"),
        quantity("BloodPressureSystolic", "2026-11-20T20:00:00", "2026-11-20T20:00:00", 116, "mmHg"),
        quantity("WaistCircumference", "2026-11-20T08:00:00", "2026-11-20T08:00:00", 0.82, "m"),
        # A night's wrist temperature counts toward the morning, and a direct source's skin temperature wins.
        quantity("AppleSleepingWristTemperature", "2026-11-20T23:30:00", "2026-11-21T06:30:00", 34.9, "degC"),
        quantity("AppleSleepingWristTemperature", "2026-11-21T23:30:00", "2026-11-22T06:30:00", 35.1, "degC"),
    ])
    whoop(store, ("recovery", "8", scored({"cycle_id": 8, "created_at": at("2026-11-21T07:00:00")},
                                          skin_temp_celsius=33.4)))
    out = review(store, 1, "2026-11-22")
    assert out["activity"] == [{"date": "2026-11-20", "avg_hr_bpm": 71.4, "exercise_min": 42, "steps": 9412,
                                "walk_run_km": 7.2}]
    assert out["mobility"] == [{"date": "2026-11-20", "walking_speed_kmh": 4.7}]
    assert out["vitals"] == [{"date": "2026-11-20", "bp_systolic_mmhg": 118}]
    assert out["body"] == [{"date": "2026-11-20", "waist_cm": 82}]
    assert [(d["date"], d["skin_temp_c"]) for d in out["recovery"]] == [("2026-11-21", 33.4), ("2026-11-22", 35.1)]
