"""What each source's records say, read from their raw payloads. Mappers are pure: the same raw always reads the
same, so changing one and rebuilding gives history the new answer."""

from __future__ import annotations

import re
from datetime import timedelta, timezone
from functools import lru_cache
from typing import Any, Callable

from .contract import Contract, instant, number, stamp

# (source_type, source_id) of another record this one reads, and its current raw or None.
Related = Callable[[str, str], "dict | None"]


def _at(ts: str, offset: str | None = None) -> str:
    """`ts` as an instant written in `offset` (such as -05:00), so its local date is the source's."""
    at = instant(ts)
    if offset and re.fullmatch(r"[+-]\d{2}:\d{2}", offset):
        sign = -1 if offset[0] == "-" else 1
        at = at.astimezone(timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:]))))
    return stamp(at)


def _seconds(start: str, end: str) -> float:
    return (instant(end) - instant(start)).total_seconds()


def _quantity(code: str, value: Any, unit: str | None, start: str, end: str | None = None) -> dict:
    out = {"code": code, "value": value, "time": {"start": start, **({"end": end} if end and end != start else {})}}
    return {**out, "unit": unit} if unit else out


def _scored(raw: dict) -> dict:
    return (raw.get("score") or {}) if raw.get("score_state") == "SCORED" else {}


# -- WHOOP ----------------------------------------------------------------------

STAGES = {"total_light_sleep_time_milli": "sleep_light_duration", "total_slow_wave_sleep_time_milli":
          "sleep_deep_duration", "total_rem_sleep_time_milli": "sleep_rem_duration",
          "total_awake_time_milli": "sleep_awake_duration"}
RECOVERY = (("recovery_score", "whoop_recovery_score", "%"), ("resting_heart_rate", "resting_heart_rate", "/min"),
            ("hrv_rmssd_milli", "hrv_rmssd", "ms"), ("spo2_percentage", "oxygen_saturation", "%"),
            ("skin_temp_celsius", "skin_temperature", "Cel"))
SPORTS = {"weightlifting_msk": "strength training", "strength-trainer": "strength training", "hiit": "HIIT",
          "hiking-rucking": "hiking", "track-field": "track and field", "f45-training": "F45 training",
          "barrys": "Barry's"}


def whoop_sleep(raw: dict, related: Related) -> dict:
    offset = raw.get("timezone_offset")
    start, end = _at(raw["start"], offset), _at(raw["end"], offset)
    found = [_quantity("sleep_stage", "in_bed", None, start, end)]
    score = _scored(raw)
    for key, code in STAGES.items():
        if (score.get("stage_summary") or {}).get(key) is not None:
            found.append(_quantity(code, score["stage_summary"][key] / 1000, "s", start, end))
    for key, code, unit in (("sleep_performance_percentage", "whoop_sleep_performance", "%"),
                            ("respiratory_rate", "respiratory_rate", "/min")):
        if score.get(key) is not None:
            found.append(_quantity(code, score[key], unit, end))
    return {"observations": found}


def _woke(related: Related, sleep_id: Any) -> str | None:
    sleep = related("sleep", str(sleep_id)) if sleep_id is not None else None
    return _at(sleep["end"], sleep.get("timezone_offset")) if sleep else None


def whoop_recovery(raw: dict, related: Related) -> dict:
    """Dated when you woke, as in the WHOOP app: the end of its sleep."""
    score = _scored(raw)
    at = _woke(related, raw.get("sleep_id")) or _at(raw["created_at"])
    return {"observations": [_quantity(code, score[key], unit, at) for key, code, unit in RECOVERY
                             if score.get(key) is not None],
            "related": [("sleep", str(raw["sleep_id"]))] if raw.get("sleep_id") is not None else []}


def whoop_cycle(raw: dict, related: Related) -> dict:
    score = _scored(raw)
    if score.get("strain") is None:
        return {"observations": []}
    return {"observations": [_quantity("whoop_strain", score["strain"], None, _at(raw["start"], raw.get("timezone_offset")))]}


def whoop_workout(raw: dict, related: Related) -> dict:
    offset = raw.get("timezone_offset")
    start, end = _at(raw["start"], offset), _at(raw["end"], offset)
    metrics: dict = {"duration": {"value": _seconds(start, end), "unit": "s"}}
    score = _scored(raw)
    for key, code, unit in (("average_heart_rate", "heart_rate", "/min"), ("max_heart_rate", "heart_rate_max", "/min"),
                            ("distance_meter", "distance", "m"), ("kilojoule", "active_energy", "kJ"),
                            ("strain", "whoop_strain", None)):
        if score.get(key) is not None:
            metrics[code] = {"value": score[key], "unit": unit} if unit else {"value": score[key]}
    sport = raw.get("sport_name") or "workout"
    return {"session": {"name": SPORTS.get(sport) or re.sub(r"[_-]+", " ", sport), "time": {"start": start, "end": end},
                        "metrics": metrics}}


# -- samples: Apple Health -------------------------------------------------------

# HealthKit units as the registry spells them.
UNITS = {"count/min": "/min", "degC": "Cel", "ml/kg*min": "mL/(kg.min)", "mmHg": "mm[Hg]", "count": None}


@lru_cache(maxsize=1)
def codes() -> dict[str, str]:
    """Each HealthKit sample type's registry code."""
    registry = Contract.shipped().registry["codes"]
    return {spec["healthkit"]: code for code, spec in registry.items() if "healthkit" in spec}


def observation(sample: dict) -> dict | None:
    """A sample's observation payload, or None for a type that isn't one."""
    code = codes().get(sample["type"])
    if code is None:
        return None
    value, unit = sample["value"], UNITS.get(sample.get("unit"), sample.get("unit"))
    if isinstance(value, str):
        value = re.sub(r"(?<!^)([A-Z]+)", r"_\1", value).lower()  # asleepREM -> asleep_rem
    payload = {"code": code, "value": value, "time": {"start": sample["start"], "end": sample.get("end", sample["start"])}}
    return {**payload, "unit": unit} if unit else payload


def sample(raw: dict, related: Related) -> dict:
    found = observation(raw)
    return {"observations": [found] if found else []}


# HealthKit activity types whose words alone don't read well. Others become their words: crossTraining -> cross training.
HEALTH_ACTIVITIES = {"traditionalStrengthTraining": "strength training",
                     "functionalStrengthTraining": "functional strength training",
                     "highIntensityIntervalTraining": "HIIT", "mixedCardio": "cardio", "stairClimbing": "stairs",
                     "other": "workout"}
HEALTH_TOTALS = (("distance_m", "distance", "m"), ("heart_rate_avg_bpm", "heart_rate", "/min"),
                 ("heart_rate_max_bpm", "heart_rate_max", "/min"), ("energy_kcal", "active_energy", "kcal"))


def health_workout(raw: dict, related: Related) -> dict:
    start = raw["start"]
    end = raw.get("end", start)
    totals = raw.get("raw") or {}
    seconds = totals.get("duration_s")
    if not number(seconds):
        seconds = _seconds(start, end)
    metrics: dict = {"duration": {"value": seconds, "unit": "s"}}
    for key, code, unit in HEALTH_TOTALS:
        if number(totals.get(key)):
            metrics[code] = {"value": totals[key], "unit": unit}
    kind = str(raw.get("value") or "other")
    name = HEALTH_ACTIVITIES.get(kind) or re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", kind).lower()
    return {"session": {"name": name, "time": {"start": start, "end": end}, "metrics": metrics}}


def _whoop_revised(raw: dict) -> str:
    return str(raw.get("updated_at") or "")


def _sample_revised(raw: dict) -> str:
    return str((raw.get("raw") or {}).get("modified_at") or raw.get("modified_at") or "")


# Health samples may arrive without a revision time; a changed one is stamped on arrival.
SAMPLES = {("apple_health", "quantity_sample"): sample, ("apple_health", "category_sample"): sample,
           ("apple_health", "workout"): health_workout}

# (source, source_type) -> (when the revision was made, as the source says; what it reads as)
MAPPERS: dict[tuple[str, str], tuple[Callable[[dict], str], Callable[[dict, Related], dict]]] = {
    ("whoop", "sleep"): (_whoop_revised, whoop_sleep),
    ("whoop", "recovery"): (_whoop_revised, whoop_recovery),
    ("whoop", "cycle"): (_whoop_revised, whoop_cycle),
    ("whoop", "workout"): (_whoop_revised, whoop_workout),
    **{key: (_sample_revised, mapper) for key, mapper in SAMPLES.items()},
}


def unstamped(raw: Any) -> Any:
    """`raw` without the time it was revised, to tell a new revision from the same one sent again."""
    if not isinstance(raw, dict):
        return raw
    out = {k: v for k, v in raw.items() if k not in ("updated_at", "modified_at")}
    if isinstance(out.get("raw"), dict):
        out["raw"] = {k: v for k, v in out["raw"].items() if k != "modified_at"}
    return out


def stamped(payload: dict, at: str) -> dict:
    """A health sample with `at` as its revision time, for a changed one sent again without a time of its own."""
    if (payload.get("source"), payload.get("source_type")) not in SAMPLES:
        return payload
    return {**payload, "raw": {**payload["raw"], "modified_at": at}}


def revised(payload: dict) -> str:
    """When a revision was made, by its own source's account; "" when the source doesn't say."""
    found = MAPPERS.get((payload.get("source"), payload.get("source_type")))
    try:
        return found[0](payload["raw"]) if found else ""
    except (KeyError, TypeError, AttributeError):
        return ""


def revision_order(event: dict) -> tuple:
    """Revisions in the order their source made them: by revision time as an instant. Ties, and revisions without
    one, go by id, which every device derives alike, so every device picks the same current revision."""
    try:
        at = instant(revised(event["payload"]))
        seconds = at.timestamp() if at.tzinfo else None
    except ValueError:
        seconds = None
    return (seconds is not None, seconds or 0.0, event["id"])


def read(payload: dict, related: Related) -> dict:
    """{observations: [...], session: {...} | None, related: [(source_type, source_id)]}: empty for a source or
    type this build has no mapper for, whose raw is still kept."""
    found = MAPPERS.get((payload["source"], payload["source_type"]))
    out = found[1](payload["raw"], related) if found else {}
    return {"observations": out.get("observations", []), "session": out.get("session"),
            "related": out.get("related", [])}
