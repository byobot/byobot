"""Small builders the scenarios share: intents and source records, synthetic values only."""

from __future__ import annotations

from .harness import World


def effort(rid: str = "e1", reps: int = 5, at: str = "2026-03-02T09:00:00.000Z", activity: str = "x.row") -> dict:
    return {"intent": "log_effort", "record": {"id": rid, "activity_id": activity, "performed_at": at,
                                               "metrics": {"reps": {"value": reps}}}}


def edit(rid: str = "e1", reps: int = 6) -> dict:
    return {"intent": "edit", "record": ["effort", rid], "fields": {"metrics": {"reps": {"value": reps}}}}


def session(rid: str, start: str, end: str) -> dict:
    return {"intent": "log_session", "record": {"id": rid, "name": "run", "started_at": start, "ended_at": end}}


def reps(world: World, name: str, rid: str = "e1") -> int | None:
    record = world.snapshot(name)["records"].get(f"effort/{rid}")
    return record and record["fields"]["metrics"]["reps"]["value"]


def both_ways(world: World, *names: str) -> None:
    for a in names:
        for b in names:
            if a != b:
                world.sync(a, b)
    for a in names:
        for b in names:
            if a != b:
                world.sync(a, b)


def whoop_workout(wid: str, start: str, end: str, updated: str, strain: float = 12.0,
                  distance: float | None = None) -> dict:
    score = {"strain": strain, "average_heart_rate": 150, "max_heart_rate": 172, "kilojoule": 2000.0,
             "percent_recorded": 100.0,
             "zone_durations": {f"zone_{n}_milli": 300_000 for n in ("zero", "one", "two", "three", "four", "five")}}
    if distance is not None:
        score["distance_meter"] = distance
    return {"id": wid, "user_id": 1, "created_at": updated, "updated_at": updated, "start": start, "end": end,
            "timezone_offset": "+00:00", "sport_name": "running", "score_state": "SCORED", "score": score}


def whoop_sleep(sid: str, cycle: int, start: str, end: str, updated: str, performance: int = 80) -> dict:
    summary = {"total_in_bed_time_milli": 28_800_000, "total_awake_time_milli": 1_800_000,
               "total_no_data_time_milli": 0,
               "total_light_sleep_time_milli": 14_000_000, "total_slow_wave_sleep_time_milli": 6_000_000,
               "total_rem_sleep_time_milli": 7_000_000, "sleep_cycle_count": 4, "disturbance_count": 8}
    need = {"baseline_milli": 27_000_000, "need_from_sleep_debt_milli": 0, "need_from_recent_strain_milli": 0,
            "need_from_recent_nap_milli": 0}
    return {"id": sid, "cycle_id": cycle, "user_id": 1, "created_at": end, "updated_at": updated, "start": start,
            "end": end, "timezone_offset": "+00:00", "nap": False, "score_state": "SCORED",
            "score": {"stage_summary": summary, "sleep_needed": need, "respiratory_rate": 15.0,
                      "sleep_performance_percentage": performance}}


def health_workout(sid: str, start: str, end: str, distance: float) -> dict:
    return {"source": "apple_health", "source_id": sid, "type": "HKWorkoutTypeIdentifier", "start": start,
            "end": end, "value": "running", "raw": {"distance_m": distance, "heart_rate_avg_bpm": 148}}


def weight(sid: str, at: str, kg: float, modified: str | None = None) -> dict:
    sample = {"source": "apple-health", "source_id": sid, "type": "HKQuantityTypeIdentifierBodyMass", "start": at, "end": at,
              "value": kg, "unit": "kg"}
    if modified:
        sample["raw"] = {"modified_at": modified}
    return sample
