"""A compact digest of recent training and recovery, so an agent can coach without writing SQL."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from byobot.core import occurrences
from byobot.core.store import Store


def readable_seconds(seconds: float) -> str:
    """45 s, 1 min 30 s, 38 min, 1 h 12 min. Seconds only show under ten minutes."""
    if seconds < 60:
        return f"{round(seconds)} s"
    if seconds < 600:
        m, s = divmod(round(seconds), 60)
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


def _compact(rows: list[dict]) -> dict:
    out = {}
    for m in rows:
        value = m["text"] if m.get("text") is not None else m.get("value")
        if value is None and (m.get("min") is not None or m.get("max") is not None):
            value = f"{m.get('min') if m.get('min') is not None else ''}-{m.get('max') if m.get('max') is not None else ''}"
        if m.get("unit") == "s" and isinstance(value, (int, float)):
            out[m["code"]] = readable_seconds(value)
        else:
            out[m["code"]] = f"{_round(value)} {m['unit']}" if m.get("unit") else _round(value)
    return out


def _round(x):
    return int(x) if isinstance(x, float) and x.is_integer() else x


# Code -> review key, and the factor from the code's own unit. A day keeps its latest value, except MEAN keys,
# which average.
RECOVERY = {"recovery_score": ("recovery_pct", 1), "hrv_rmssd": ("hrv_rmssd_ms", 1), "hrv_sdnn": ("hrv_sdnn_ms", 1),
            "resting_heart_rate": ("resting_hr_bpm", 1), "sleep_performance": ("sleep_performance_pct", 1),
            "strain": ("strain", 1), "skin_temperature": ("skin_temp_c", 1),
            "sleeping_wrist_temperature": ("skin_temp_c", 1), "respiratory_rate": ("respiratory_rate_bpm", 1),
            "oxygen_saturation": ("spo2_pct", 1)}
BODY = {"body_mass": ("weight_kg", 1), "body_fat_percentage": ("body_fat_pct", 1),
        "lean_body_mass": ("lean_mass_kg", 1), "vo2_max": ("vo2max", 1), "body_mass_index": ("bmi", 1),
        "waist_circumference": ("waist_cm", 100), "height": ("height_cm", 100)}
ACTIVITY = {"step_count": ("steps", 1), "distance_walking_running": ("walk_run_km", 0.001),
            "distance_cycling": ("cycling_km", 0.001), "distance_swimming": ("swimming_km", 0.001),
            "flights_climbed": ("flights", 1), "active_energy": ("active_kcal", 1),
            "basal_energy": ("resting_kcal", 1), "exercise_time": ("exercise_min", 1 / 60),
            "stand_time": ("stand_min", 1 / 60), "heart_rate": ("avg_hr_bpm", 1),
            "walking_heart_rate_average": ("walking_hr_bpm", 1),
            "heart_rate_recovery_one_minute": ("hr_recovery_1min_bpm", 1)}
MOBILITY = {"walking_speed": ("walking_speed_kmh", 3.6), "walking_step_length": ("step_length_cm", 100),
            "walking_double_support_percentage": ("double_support_pct", 1),
            "walking_asymmetry_percentage": ("walking_asymmetry_pct", 1),
            "walking_steadiness": ("walking_steadiness_pct", 1),
            "six_minute_walk_test_distance": ("six_minute_walk_m", 1), "stair_ascent_speed": ("stair_up_kmh", 3.6),
            "stair_descent_speed": ("stair_down_kmh", 3.6)}
VITALS = {"blood_pressure_systolic": ("bp_systolic_mmhg", 1), "blood_pressure_diastolic": ("bp_diastolic_mmhg", 1),
          "blood_glucose": ("glucose_mg_dl", 1), "body_temperature": ("body_temp_c", 1)}
KEYS = {**RECOVERY, **BODY, **ACTIVITY, **MOBILITY, **VITALS}
MEAN = {k for k, _ in (*MOBILITY.values(), *VITALS.values())} | {"hr_recovery_1min_bpm"}
# Measured overnight, so they count toward the morning, like sleep.
OVERNIGHT = {"sleeping_wrist_temperature"}
ASLEEP = ("sleep_light_duration", "sleep_deep_duration", "sleep_rem_duration")
ASLEEP_STAGES = ("asleep_unspecified", "asleep_core", "asleep_deep", "asleep_rem")
# Keys that measure the same thing, so a day takes them from one source.
SAME = {"hrv_sdnn_ms": "hrv", "hrv_rmssd_ms": "hrv"}


def _night(ended_at: str) -> str:
    """The day a night belongs to, in the offset it was recorded in: one ending in the evening counts toward
    the next morning."""
    return (datetime.fromisoformat(ended_at.replace("Z", "+00:00")) + timedelta(hours=6)).date().isoformat()


def _daily(store: Store, lo: str, hi: str) -> dict[str, dict]:
    """Each day's values by review key, each from one source: yours, then sources in source_priority order.

    A source's night of sleep comes from the one device that recorded the most of it.
    """
    order, _ = occurrences.priority(store.conn)
    codes = [*KEYS, *ASLEEP, "sleep_stage"]
    rows = store.rows(
        "SELECT o.code, o.std_value, o.text, o.started_at, o.ended_at, o.start_ms, o.end_ms, o.local_date, o.source,"
        " coalesce(json_extract(s.raw, '$.device'), '') AS device FROM observations o"
        " LEFT JOIN source_records s ON s.id = o.source_record_id"
        f" WHERE o.code IN ({', '.join('?' for _ in codes)}) AND o.local_date >= ? AND o.local_date < ?"
        " ORDER BY o.start_ms, o.id", [*codes, lo, hi])
    found: dict[tuple, dict] = defaultdict(lambda: defaultdict(list))  # (day, group) -> rank -> [(key, value)]
    nights: dict[tuple, dict] = defaultdict(lambda: defaultdict(float))  # (day, rank) -> device -> seconds
    for r in rows:
        rank = occurrences.rank(r["source"], order)
        if r["code"] == "sleep_stage":
            if r["text"] in ASLEEP_STAGES and r["end_ms"]:
                nights[(_night(r["ended_at"]), rank)][r["device"]] += (r["end_ms"] - r["start_ms"]) / 1000
            continue
        if r["std_value"] is None:
            continue
        if r["code"] in ASLEEP:
            day, key, value = _night(r["ended_at"] or r["started_at"]), "asleep_h", r["std_value"] / 3600
        else:
            key, factor = KEYS[r["code"]]
            day = _night(r["ended_at"] or r["started_at"]) if r["code"] in OVERNIGHT else r["local_date"]
            value = r["std_value"] * factor
        found[(day, SAME.get(key, key))][rank].append((key, value))
    for (day, rank), devices in nights.items():
        if not found[(day, "asleep_h")][rank]:
            found[(day, "asleep_h")][rank].append(("asleep_h", max(devices.values()) / 3600))
    days: dict[str, dict] = defaultdict(dict)
    for (day, _), ranks in found.items():
        values: dict[str, list] = defaultdict(list)
        for key, value in ranks[min(ranks)]:
            values[key].append(value)
        for key, v in values.items():
            days[day][key] = sum(v) if key == "asleep_h" else sum(v) / len(v) if key in MEAN else v[-1]
    return days


def _by_day(days: dict[str, dict], keys: set[str], first: str, last: str) -> list[dict]:
    out = []
    for d in sorted(days):
        values = {k: round(v, 1) for k, v in sorted(days[d].items()) if k in keys}
        if values and first <= d <= last:
            out.append({"date": d, **values})
    return out


def _metric_rows(store: Store, kind: str, rid: str) -> list[dict]:
    return store.rows("SELECT code, value, min, max, text, unit FROM metrics WHERE owner_kind = ? AND owner_id = ?"
                      " AND part_id = '' ORDER BY code", (kind, rid))


def review(store: Store, weeks: int, until: str | None = None) -> dict:
    end = date.fromisoformat(until) if until else date.today()
    start = end - timedelta(weeks=weeks)
    first, last = start.isoformat(), end.isoformat()
    after = (end + timedelta(days=1)).isoformat()

    targets = {r["id"]: json.loads(r["targets"]) if r["targets"] else {}
               for r in store.rows("SELECT id, targets FROM activities")}
    efforts = store.rows(
        "SELECT e.id, e.activity_id, e.local_date, e.side, coalesce(e.location_id, s.location_id) AS location_id"
        " FROM efforts e LEFT JOIN sessions s ON s.id = e.session_id"
        " WHERE e.local_date >= ? AND e.local_date <= ? ORDER BY e.start_ms, e.id", [first, last])

    occasions: dict[tuple, dict] = {}
    weekly: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"main": 0, "synergist": 0}))
    for e in efforts:
        day = e["local_date"]
        sets = occasions.setdefault((e["activity_id"], e["location_id"]), {}).setdefault(day, [])
        entry = _compact(_metric_rows(store, "effort", e["id"]))
        if e["side"]:
            entry["side"] = e["side"]
        sets.append(entry)
        week = (date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())).isoformat()
        for role, key in (("main", "main"), ("synergists", "synergist")):
            for muscle in targets.get(e["activity_id"], {}).get(role, []):
                weekly[week][muscle][key] += 1

    exercises = []
    for (activity_id, location_id), days in sorted(occasions.items(), key=lambda kv: -len(kv[1])):
        recent = sorted(days)[-2:]
        exercises.append({"activity_id": activity_id, "location_id": location_id, "days_done": len(days),
                          "recent": [{"date": d, "sets": days[d]} for d in recent]})

    workouts = []
    for o in store.rows("SELECT * FROM occurrences WHERE local_date >= ? AND local_date <= ? ORDER BY start_ms, id",
                        [first, last]):
        items = store.rows("SELECT i.kind, i.id, r.source FROM occurrence_items i LEFT JOIN source_records r"
                           " ON i.kind = 'source_record' AND r.id = i.id WHERE i.occurrence_id = ?", [o["id"]])
        mine = [i["id"] for i in items if i["kind"] == "session"]
        session = store.rows("SELECT location_id, planned_session_id FROM sessions WHERE id = ?", mine[:1]) if mine \
            else []
        entry = {"date": o["local_date"], "name": o["name"], **(session[0] if session else {})}
        if mine:
            entry["sets"] = store.rows("SELECT count(*) AS n FROM efforts WHERE session_id = ?", mine[:1])[0]["n"]
        found = _compact(store.rows("SELECT code, value, NULL AS min, NULL AS max, text, unit FROM occurrence_metrics"
                                    " WHERE occurrence_id = ? ORDER BY code", [o["id"]]))
        if found:
            entry["metrics"] = found
        from_sources = sorted({i["source"] for i in items if i["source"]})
        if from_sources:
            entry["sources"] = from_sources
        workouts.append(entry)

    done = {r["p"] for r in store.rows("SELECT planned_session_id AS p FROM sessions WHERE planned_session_id IS NOT NULL"
                                       " UNION SELECT planned_session_id FROM efforts WHERE planned_session_id IS NOT NULL")}
    planned = store.rows("SELECT id, scheduled_on, name, plan_id, location_id, status FROM planned_sessions"
                         " WHERE scheduled_on >= ? ORDER BY scheduled_on, id", [first])
    past = [p for p in planned if p["scheduled_on"] < last and p["status"] != "skipped"]
    daily = _daily(store, (start - timedelta(days=1)).isoformat(), after)
    return {
        "from": first,
        "to": last,
        "sessions": workouts,
        "plan_adherence": {
            "planned": len(past),
            "done": sum(p["id"] in done for p in past),
            "missed": [{k: p[k] for k in ("id", "scheduled_on", "name")} for p in past if p["id"] not in done],
        },
        "exercises": exercises,
        "weekly_sets_per_muscle": {w: dict(sorted(m.items())) for w, m in sorted(weekly.items())},
        "recovery": _by_day(daily, {*(k for k, _ in RECOVERY.values()), "asleep_h"}, first, last),
        "body": _by_day(daily, {k for k, _ in BODY.values()}, first, last),
        "activity": _by_day(daily, {k for k, _ in ACTIVITY.values()}, first, last),
        "mobility": _by_day(daily, {k for k, _ in MOBILITY.values()}, first, last),
        "vitals": _by_day(daily, {k for k, _ in VITALS.values()}, first, last),
        "upcoming": [p for p in planned if p["scheduled_on"] >= last],
    }
