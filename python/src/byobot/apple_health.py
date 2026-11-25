"""Apple Health, read by the iPhone app and pushed to `byobot serve` as telemetry.

Workouts arrive as samples and become performed sessions here, merged with the sessions you log.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from byobot import imports

NAME = "apple-health"
ACTOR = {"kind": "connector", "id": NAME}
WORKOUT = "HKWorkoutTypeIdentifier"

# HealthKit activity types whose words alone don't read well. Others become their words: crossTraining -> cross training.
ACTIVITIES = {
    "traditionalStrengthTraining": "strength training",
    "functionalStrengthTraining": "functional strength training",
    "highIntensityIntervalTraining": "HIIT",
    "mixedCardio": "cardio",
    "stairClimbing": "stairs",
    "other": "workout",
}


def activity(code: str) -> str:
    return ACTIVITIES.get(code) or re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", code).lower()


def session(row: dict) -> dict:
    totals = json.loads(row["payload"]).get("raw") or {}
    start, end = imports.utc(row["start_at"]), imports.utc(row["end_at"])
    seconds = totals.get("duration_s")
    if seconds is None:
        seconds = (imports._at(end) - imports._at(start)).total_seconds()
    metrics: dict = {"duration": {"value": seconds, "unit": "s"}}
    for key, metric, unit in (("heart_rate_avg_bpm", "heart_rate", "bpm"),
                              ("heart_rate_max_bpm", "x.max_heart_rate", "bpm"),
                              ("distance_m", "distance", "m"),
                              ("energy_kcal", "x.calories", "kcal")):
        if isinstance(totals.get(key), (int, float)) and not isinstance(totals[key], bool):
            metrics[metric] = {"value": round(totals[key], 1), "unit": unit}
    metrics["x.healthkit_activity"] = {"value": row["category"]}
    return {"id": f"{NAME}:workout:{row['source_id']}", "name": activity(row["category"]),
            "started_at": start, "ended_at": end, "metrics": metrics}


def workouts(store) -> list[dict]:
    """Every Apple Health workout as a session. Where two apps wrote the same workout, the longer one stays."""
    rows = store.rows("SELECT t.source_id, t.start_at, t.end_at, t.category, r.payload FROM telemetry_samples t"
                      " JOIN raw_records r ON r.id = t.raw_record_id WHERE t.source = ? AND t.type = ?",
                      [NAME, WORKOUT])
    kept: list[dict] = []
    for s in sorted(map(session, rows), key=lambda s: (-s["metrics"]["duration"]["value"], s["id"])):
        if all(imports.overlap(s["started_at"], s["ended_at"], k["started_at"], k["ended_at"]) <= timedelta(0)
               for k in kept):
            kept.append(s)
    return kept


def save_sessions(store) -> dict:
    """Bring the sessions in line with the Apple Health workouts the phone has sent."""
    return imports.save_sessions(store, workouts(store), ACTOR, complete=True)
