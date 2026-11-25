"""Analysis views: one wide row per day, per exercise day or per muscle week, for people and agents writing SQL.

They read the derived tables, so they need no events and no rebuild. Dates are local dates on the computer
running byobot.
"""

from __future__ import annotations

import hashlib

# Telemetry type -> daily column. Durations add up per day, MEAN columns average, everything else keeps the day's latest.
RECOVERY = {
    "byobot.RecoveryScore": "recovery_pct",
    "byobot.HeartRateVariabilityRMSSD": "hrv_rmssd_ms",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn_ms",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr_bpm",
    "byobot.SleepPerformance": "sleep_performance_pct",
    "byobot.Strain": "strain",
    "byobot.SkinTemperature": "skin_temp_c",
    "HKQuantityTypeIdentifierAppleSleepingWristTemperature": "skin_temp_c",
}
BODY = {
    "HKQuantityTypeIdentifierBodyMass": "weight_kg",
    "HKQuantityTypeIdentifierBodyFatPercentage": "body_fat_pct",
    "HKQuantityTypeIdentifierLeanBodyMass": "lean_mass_kg",
    "HKQuantityTypeIdentifierVO2Max": "vo2max",
    "HKQuantityTypeIdentifierBodyMassIndex": "bmi",
    "HKQuantityTypeIdentifierWaistCircumference": "waist_cm",
    "HKQuantityTypeIdentifierHeight": "height_cm",
}
ACTIVITY = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "walk_run_km",
    "HKQuantityTypeIdentifierDistanceCycling": "cycling_km",
    "HKQuantityTypeIdentifierDistanceSwimming": "swimming_km",
    "HKQuantityTypeIdentifierFlightsClimbed": "flights",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_kcal",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "resting_kcal",
    "HKQuantityTypeIdentifierAppleExerciseTime": "exercise_min",
    "HKQuantityTypeIdentifierAppleStandTime": "stand_min",
    "HKQuantityTypeIdentifierHeartRate": "avg_hr_bpm",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_hr_bpm",
    "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute": "hr_recovery_1min_bpm",
}
MOBILITY = {
    "HKQuantityTypeIdentifierWalkingSpeed": "walking_speed_kmh",
    "HKQuantityTypeIdentifierWalkingStepLength": "step_length_cm",
    "HKQuantityTypeIdentifierWalkingDoubleSupportPercentage": "double_support_pct",
    "HKQuantityTypeIdentifierWalkingAsymmetryPercentage": "walking_asymmetry_pct",
    "HKQuantityTypeIdentifierAppleWalkingSteadiness": "walking_steadiness_pct",
    "HKQuantityTypeIdentifierSixMinuteWalkTestDistance": "six_minute_walk_m",
    "HKQuantityTypeIdentifierStairAscentSpeed": "stair_up_kmh",
    "HKQuantityTypeIdentifierStairDescentSpeed": "stair_down_kmh",
}
VITALS = {
    "HKQuantityTypeIdentifierBloodPressureSystolic": "bp_systolic_mmhg",
    "HKQuantityTypeIdentifierBloodPressureDiastolic": "bp_diastolic_mmhg",
    "HKQuantityTypeIdentifierBloodGlucose": "glucose_mg_dl",
    "HKQuantityTypeIdentifierBodyTemperature": "body_temp_c",
}
KEYS = {**RECOVERY, **BODY, **ACTIVITY, **MOBILITY, **VITALS}
# From the contract's unit to the column's.
SCALE = {"waist_cm": 100, "height_cm": 100, "step_length_cm": 100, "walk_run_km": 0.001, "cycling_km": 0.001,
         "swimming_km": 0.001, "exercise_min": 1 / 60, "stand_min": 1 / 60, "walking_speed_kmh": 3.6,
         "stair_up_kmh": 3.6, "stair_down_kmh": 3.6}
# Measured several times a day, where the day's average says more than its last reading.
MEAN = {*MOBILITY.values(), *VITALS.values(), "hr_recovery_1min_bpm"}
# Measured overnight, so they count toward the morning, like sleep.
OVERNIGHT = {"HKQuantityTypeIdentifierAppleSleepingWristTemperature"}
ASLEEP = ("byobot.SleepLightDuration", "byobot.SleepDeepDuration", "byobot.SleepREMDuration")
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
ASLEEP_STAGES = ("asleepUnspecified", "asleepCore", "asleepDeep", "asleepREM")
# Columns that measure the same thing, so a day takes them from one source.
SAME = {"hrv_sdnn_ms": "hrv", "hrv_rmssd_ms": "hrv"}
# Apple Health collects what other apps write, including copies of sources byobot reads directly.
COLLECTED = ("apple-health",)

DAILY_TELEMETRY = list(dict.fromkeys([*RECOVERY.values(), "asleep_h", *BODY.values(), *ACTIVITY.values(),
                                      *MOBILITY.values(), *VITALS.values()]))


def _text(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _daily_sql() -> str:
    keys = [(t, k, SAME.get(k, k), SCALE.get(k, 1), "mean" if k in MEAN else "last",
             "night" if t in OVERNIGHT else "start") for t, k in KEYS.items()]
    keys += [(t, "asleep_h", "asleep_h", 1 / 3600, "sum", "end") for t in ASLEEP]
    values = ",\n  ".join(f"('{t}', '{k}', '{g}', {s!r}, '{h}', '{d}')" for t, k, g, s, h, d in keys)
    pivot = ",\n  ".join(f"max(CASE WHEN key = '{k}' THEN value END) AS {k}" for k in DAILY_TELEMETRY)
    return f"""-- One row per local date: recovery, sleep, body, activity, mobility and vitals, then training.
-- Each value comes from one source: one byobot reads directly beats Apple Health's copy of it.
-- Sleep counts toward the morning it ends. Training counts every logged effort; volume is reps times kg.
CREATE VIEW daily AS
WITH keys (type, key, grp, scale, how, day_from) AS (VALUES
  {values}
),
readings AS (
  SELECT k.key, k.grp, k.how, t.source IN ({_text(COLLECTED)}) AS collected, t.start_at, t.end_at, t.rowid AS n,
    CASE k.day_from WHEN 'start' THEN date(t.start_at, 'localtime') WHEN 'end' THEN date(t.end_at, 'localtime')
      ELSE date(t.end_at, 'localtime', '+6 hours') END AS date,
    t.value * k.scale AS value
  FROM telemetry_samples t JOIN keys k ON k.type = t.type
  WHERE t.value IS NOT NULL
  UNION ALL
  -- Apple Health sleep: each night from the one app or device that recorded the most of it.
  SELECT 'asleep_h', 'asleep_h', 'sum', 1, NULL, NULL, NULL, date, max(hours)
  FROM (SELECT date(end_at, 'localtime', '+6 hours') AS date, coalesce(device_id, '') AS device,
          sum(strftime('%s', end_at) - strftime('%s', start_at)) / 3600.0 AS hours
        FROM telemetry_samples
        WHERE type = '{SLEEP}' AND source IN ({_text(COLLECTED)}) AND category IN ({_text(ASLEEP_STAGES)})
        GROUP BY 1, 2)
  GROUP BY date
),
ranked AS (
  SELECT *, min(collected) OVER (PARTITION BY date, grp) AS best,
    row_number() OVER (PARTITION BY date, key, collected ORDER BY start_at DESC, end_at DESC, n DESC) AS latest
  FROM readings
),
telemetry AS (
  SELECT date, key, CASE how WHEN 'sum' THEN sum(value) WHEN 'mean' THEN avg(value)
    ELSE max(CASE WHEN latest = 1 THEN value END) END AS value
  FROM ranked WHERE collected = best GROUP BY date, key, how
),
days AS (
  SELECT date,
  {pivot}
  FROM telemetry GROUP BY date
),
sessions AS (
  SELECT date(started_at, 'localtime') AS date, count(*) AS training_sessions,
    sum(strftime('%s', ended_at) - strftime('%s', started_at)) / 60.0 AS training_min
  FROM performed_sessions GROUP BY 1
),
efforts AS (
  SELECT date(e.performed_at, 'localtime') AS date, count(*) AS training_sets,
    coalesce(sum(s.reps * s.load_kg), 0) AS training_volume_kg
  FROM performed_efforts e LEFT JOIN strength_sets s ON s.effort_id = e.id GROUP BY 1
),
dates AS (SELECT date FROM days UNION SELECT date FROM sessions UNION SELECT date FROM efforts)
SELECT dates.date, {", ".join(f"days.{k}" for k in DAILY_TELEMETRY)},
  coalesce(sessions.training_sessions, 0) AS training_sessions, sessions.training_min,
  coalesce(efforts.training_sets, 0) AS training_sets, coalesce(efforts.training_volume_kg, 0) AS training_volume_kg
FROM dates
LEFT JOIN days ON days.date = dates.date
LEFT JOIN sessions ON sessions.date = dates.date
LEFT JOIN efforts ON efforts.date = dates.date;
"""


EXERCISE_PROGRESS = """-- One row per exercise, location and local date: how much was done and the best of it.
-- Loads compare only within one location_id. best_e1rm_kg is Epley's estimate from sets of 1 to 12 reps.
CREATE VIEW exercise_progress AS
SELECT date(s.performed_at, 'localtime') AS date, s.activity_id, a.name AS activity_name, s.location_id,
  count(*) AS sets, sum(s.reps) AS total_reps, max(s.reps) AS max_reps, max(s.load_kg) AS top_load_kg,
  coalesce(sum(s.reps * s.load_kg), 0) AS volume_kg,
  max(CASE WHEN s.reps BETWEEN 1 AND 12 THEN s.load_kg * (1 + s.reps / 30.0) END) AS best_e1rm_kg,
  avg(s.rpe) AS avg_rpe
FROM strength_sets s LEFT JOIN activities a ON a.id = s.activity_id
GROUP BY 1, s.activity_id, s.location_id;
"""

ANALYSIS_DDL = _daily_sql() + EXERCISE_PROGRESS
ANALYSIS_OBJECTS = [("view", "daily"), ("view", "exercise_progress")]


def fingerprint() -> str:
    """Changes whenever an analysis view changes, so stores recreate them on open."""
    return hashlib.sha256(ANALYSIS_DDL.encode()).hexdigest()
