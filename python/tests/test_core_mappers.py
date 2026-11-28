"""Apple Health samples in the registry's vocabulary."""

from byobot.core import mappers
from byobot.core.contract import Contract

AT = {"start": "2026-03-02T07:00:00Z", "end": "2026-03-02T07:00:00Z"}


def test_every_mapped_type_gives_an_observation_the_registry_accepts():
    c = Contract.shipped()
    for name, code in mappers.codes().items():
        spec = c.registry["codes"][code]
        value = spec["values"][0] if spec.get("value") == "category" else 1
        payload = mappers.observation({"type": name, "value": value, "unit": spec.get("unit"), **AT})
        assert payload, name
        assert c.refusals("observation", {**payload, "modality": "sensed"}) == [], name
        assert c.canonical(payload["code"], payload) in (1, None), name


def test_every_healthkit_type_in_the_registry_maps():
    registry = Contract.shipped().registry["codes"]
    assert {s["healthkit"] for s in registry.values() if "healthkit" in s} <= set(mappers.codes())


def test_healthkit_units_take_the_registry_spelling():
    c = Contract.shipped()
    for unit, ucum in mappers.UNITS.items():
        code = next((k for k, s in c.registry["codes"].items() if s.get("unit") == ucum and "healthkit" in s), None)
        if ucum is None or code is None:
            continue
        payload = mappers.observation({"type": next(t for t, k in mappers.codes().items() if k == code),
                                         "value": 1, "unit": unit, **AT})
        assert payload["unit"] == ucum, unit


def test_sleep_stages_take_the_registry_values():
    stages = Contract.shipped().registry["codes"]["sleep_stage"]["values"]
    for value in stages:
        camel = value.split("_")[0] + "".join(p.upper() if p == "rem" else p.title() for p in value.split("_")[1:])
        for sent in (value, camel):
            payload = mappers.observation({"type": "HKCategoryTypeIdentifierSleepAnalysis", "value": sent, **AT})
            assert payload["value"] == value, sent
