"""Apple Health sample types in the registry's vocabulary: each type's code, and its unit as the registry spells it."""

from __future__ import annotations

import re

from .contract import Contract

# byobot's own types; HealthKit ones map through the registry's `healthkit` field.
OWN = {
    "byobot.HeartRateVariabilityRMSSD": "hrv_rmssd", "byobot.SkinTemperature": "skin_temperature",
    "byobot.SleepLightDuration": "sleep_light_duration", "byobot.SleepDeepDuration": "sleep_deep_duration",
    "byobot.SleepREMDuration": "sleep_rem_duration", "byobot.SleepAwakeDuration": "sleep_awake_duration",
    "byobot.WhoopSleepPerformance": "whoop_sleep_performance", "byobot.WhoopRecoveryScore": "whoop_recovery_score",
    "byobot.WhoopStrain": "whoop_strain",
}
UNITS = {"count/min": "/min", "degC": "Cel", "ml/kg*min": "mL/(kg.min)", "mmHg": "mm[Hg]", "count": None}
# Workouts become sessions, not observations.
SESSIONS = {"HKWorkoutTypeIdentifier"}


def codes(contract: Contract | None = None) -> dict[str, str]:
    registry = (contract or Contract.shipped()).registry["codes"]
    return {**{spec["healthkit"]: code for code, spec in registry.items() if "healthkit" in spec}, **OWN}


def observation(sample: dict, contract: Contract | None = None) -> dict | None:
    """A sample's observation payload, or None for a type that isn't one."""
    code = codes(contract).get(sample["type"])
    if code is None:
        return None
    value, unit = sample["value"], UNITS.get(sample.get("unit"), sample.get("unit"))
    if isinstance(value, str):
        value = re.sub(r"(?<!^)([A-Z]+)", r"_\1", value).lower()  # asleepREM -> asleep_rem
    payload = {"code": code, "value": value, "time": {"start": sample["start"], "end": sample["end"]},
               "modality": "sensed"}
    return {**payload, "unit": unit} if unit else payload
