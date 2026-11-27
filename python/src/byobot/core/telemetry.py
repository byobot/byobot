"""Apple Health sample types in the registry's vocabulary: each type's code, and its unit as the registry spells it."""

from __future__ import annotations

import re

from .contract import Contract

UNITS = {"count/min": "/min", "degC": "Cel", "ml/kg*min": "mL/(kg.min)", "mmHg": "mm[Hg]", "count": None}


def codes(contract: Contract | None = None) -> dict[str, str]:
    registry = (contract or Contract.shipped()).registry["codes"]
    return {spec["healthkit"]: code for code, spec in registry.items() if "healthkit" in spec}


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
