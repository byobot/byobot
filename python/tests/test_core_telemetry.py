"""v0 telemetry in v1's vocabulary."""

import json

from byobot.core import telemetry
from byobot.core.contract import Contract, schema_dir

V0 = json.loads((schema_dir().parent / "v0" / "telemetry.schema.json").read_text())


def v0_units() -> dict:
    units = {}
    for rule in V0["allOf"]:
        t = rule["if"]["properties"]["type"]
        for name in t.get("enum") or [t["const"]]:
            units[name] = rule["then"].get("properties", {}).get("unit", {}).get("const")
    return units


def test_every_v0_type_maps_to_an_observation_code_and_unit_the_registry_accepts():
    c, units = Contract.shipped(), v0_units()
    for name in V0["properties"]["type"]["enum"]:
        if name in telemetry.SESSIONS:
            continue
        value = "asleepREM" if c.registry["codes"].get(telemetry.codes().get(name), {}).get("value") == "category" \
            else 1
        sample = {"type": name, "value": value, "unit": units.get(name), "start": "2026-03-02T07:00:00Z",
                  "end": "2026-03-02T07:00:00Z"}
        payload = telemetry.observation(sample)
        assert payload, name
        assert c.refusals("observation", payload) == [], name
        assert c.canonical(payload["code"], payload) in (1, None), name


def test_sleep_stages_take_the_registry_values():
    stages = Contract.shipped().registry["codes"]["sleep_stage"]["values"]
    names = next(r for r in V0["allOf"] if r["if"]["properties"]["type"].get("const") ==
                 "HKCategoryTypeIdentifierSleepAnalysis")["then"]["properties"]["value"]["enum"]
    seen = [telemetry.observation({"type": "HKCategoryTypeIdentifierSleepAnalysis", "value": n,
                                   "start": "2026-03-02T01:00:00Z", "end": "2026-03-02T02:00:00Z"})["value"]
            for n in names]
    assert seen == stages
