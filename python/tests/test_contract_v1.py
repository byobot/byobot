"""The schema/v1 contract: its schemas, registry and fixtures agree with each other."""

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "schema" / "v1").is_dir())
V1 = ROOT / "schema" / "v1"
FIXTURES = ROOT / "schema" / "fixtures" / "v1"
SCHEMAS = {n: json.loads((V1 / f"{n}.schema.json").read_text()) for n in ("common", "records", "event", "registry")}
REG = json.loads((V1 / "registry.json").read_text())
CASES = json.loads((FIXTURES / "cases.json").read_text())["cases"]
IDS = json.loads((FIXTURES / "ids.json").read_text())
RULES = {"time-order", "date-order", "range-order", "code-value", "part-ids", "self-reference", "derived-id"}

_resources = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in SCHEMAS.values())
EVENT = Draft202012Validator(SCHEMAS["event"], registry=_resources)
LATER = Draft202012Validator({"$ref": SCHEMAS["event"]["$id"] + "#/$defs/later"}, registry=_resources)


def jcs(value):
    # RFC 8785 for the values the fixtures use: sorted keys, no whitespace, plain numbers.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def derived(namespace, parts):
    return str(uuid.uuid5(uuid.UUID(REG["namespaces"][namespace]), jcs(parts)))


def mac(key, payload):
    return hmac.new(bytes.fromhex(key), jcs(payload).encode(), hashlib.sha256).hexdigest()


def instant(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def measures(kind, payload):
    if kind in ("session", "effort", "planned_session"):
        yield payload.get("metrics", {})
    if kind == "planned_session":
        for block in payload["blocks"]:
            yield block.get("metrics", {})
            for effort in block["efforts"]:
                yield effort.get("metrics", {})
    if kind == "overlay":
        yield payload.get("fields", {}).get("metrics", {})
    if kind == "observation":
        yield {payload["code"]: {k: payload[k] for k in ("value", "unit") if k in payload}}


def code_ok(code, m):
    spec = REG["codes"].get(code)
    if spec is None:
        return True
    kind = spec["value"]
    if (kind == "quantity") != ("unit" in m):
        return False
    numbers = [m[k] for k in ("value", "min", "max") if k in m]
    if kind in ("quantity", "integer", "number"):
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in numbers):
            return False
        if kind == "integer" and any(x != int(x) for x in numbers):
            return False
    if kind == "text" and not (isinstance(m.get("value"), str) and re.fullmatch(spec["pattern"], m["value"])):
        return False
    if kind == "category" and not isinstance(m.get("value"), str):
        return False
    factor = spec["units"].get(m["unit"]) if kind == "quantity" else 1
    if factor is None or kind in ("text", "category"):
        return True
    for x in (n * factor for n in numbers):
        if x < spec.get("minimum", x) or x > spec.get("maximum", x) or x <= spec.get("exclusive_minimum", x - 1):
            return False
    return True


def broken_rules(event):
    broken = set()
    if event["id"] in event["prev"] or event.get("reverts") == event["id"]:
        broken.add("self-reference")
    if event["op"] != "put" or "redacted_by" in event:
        return broken
    kind, rid, p = event["record"]["kind"], event["record"]["id"], event["payload"]
    for t in (p.get("time"), p.get("fields", {}).get("time")):
        if t and "end" in t and instant(t["end"]) < instant(t["start"]):
            broken.add("time-order")
    if kind == "plan" and "ends_on" in p and p["ends_on"] < p["starts_on"]:
        broken.add("date-order")
    for metrics in measures(kind, p):
        for code, m in metrics.items():
            if "min" in m and "max" in m and m["min"] > m["max"]:
                broken.add("range-order")
            if not code_ok(code, m):
                broken.add("code-value")
    if kind == "planned_session":
        parts = [b["id"] for b in p["blocks"]] + [e["id"] for b in p["blocks"] for e in b["efforts"]]
        if len(parts) != len(set(parts)):
            broken.add("part-ids")
    if kind == "source_record" and rid != derived("source_record", [p["source"], p["source_type"], p["source_id"]]):
        broken.add("derived-id")
    if kind == "overlay" and rid != derived("overlay", [p["effect"], p["target"]["kind"], p["target"]["id"]]):
        broken.add("derived-id")
    return broken


def outcome(event):
    if isinstance(event.get("schema_version"), int) and event["schema_version"] > 1:
        return "rejected" if any(LATER.iter_errors(event)) else "stored_unprojected"
    if any(EVENT.iter_errors(event)) or broken_rules(event):
        return "rejected"
    kind, op = event["record"]["kind"], event["op"]
    known = REG["kinds"].get(kind)
    if known is None or (op == "put" and event["kind_version"] > known["version"]):
        return "stored_unprojected"
    if kind == "setting" and op == "put" and event["record"]["id"] not in REG["settings"]:
        return "stored_unprojected"
    return "accepted"


def test_schemas_and_registry_are_valid():
    for doc in SCHEMAS.values():
        Draft202012Validator.check_schema(doc)
    errors = list(Draft202012Validator(SCHEMAS["registry"], registry=_resources).iter_errors(REG))
    assert not errors, [e.message for e in errors]


def test_registry_matches_the_record_schemas():
    defs = SCHEMAS["records"]["$defs"]
    assert set(REG["kinds"]) <= set(defs)
    assert set(REG["settings"]) <= set(defs)
    healthkit = [c["healthkit"] for c in REG["codes"].values() if "healthkit" in c]
    assert len(healthkit) == len(set(healthkit))
    assert all(c["units"][c["unit"]] == 1 for c in REG["codes"].values() if c["value"] == "quantity")


@pytest.mark.parametrize("c", CASES, ids=[c["name"] for c in CASES])
def test_shared_fixture(c):
    assert outcome(c["data"]) == c["outcome"]
    if c.get("rule"):
        assert not list(EVENT.iter_errors(c["data"])), "a rule case must pass the schemas"
        assert broken_rules(c["data"]) == {c["rule"]}


def test_fixtures_cover_every_kind_setting_outcome_and_rule():
    accepted = [c["data"] for c in CASES if c["outcome"] == "accepted"]
    assert {e["record"]["kind"] for e in accepted if e["op"] == "put"} == set(REG["kinds"])
    assert {e["record"]["id"] for e in accepted if e["record"]["kind"] == "setting"} == set(REG["settings"])
    assert {e["op"] for e in accepted} == {"put", "retract", "erase"}
    assert {c["outcome"] for c in CASES} == {"accepted", "stored_unprojected", "rejected"}
    assert {c["rule"] for c in CASES if "rule" in c} == RULES
    assert len({c["name"] for c in CASES}) == len(CASES)
    assert all(c["description"] for c in CASES)


def test_rules_match_the_readme():
    readme = (V1 / "README.md").read_text().split("## Rules beyond the schemas")[1]
    assert set(re.findall(r"^- `([a-z-]+)`:", readme, re.M)) == RULES


def test_derived_ids():
    key = IDS["store_key"]
    for s in IDS["source_records"]:
        p = s["payload"]
        assert s["record_id"] == derived("source_record", [p["source"], p["source_type"], p["source_id"]])
        assert s["mac"] == mac(key, p)
        assert s["put_event_id"] == derived("source_revision", [s["record_id"], s["mac"]])
        assert s["retract_event_id"] == derived("source_deletion", [s["record_id"], s["put_event_id"]])
    for o in IDS["overlays"]:
        assert o["record_id"] == derived("overlay", [o["effect"], o["target"]["kind"], o["target"]["id"]])


def test_fixture_source_events_use_the_derived_ids():
    by_record = {s["record_id"]: s for s in IDS["source_records"]}
    for c in CASES:
        e = c["data"]
        if c["outcome"] == "accepted" and e["record"]["kind"] == "source_record" and e["op"] != "erase":
            s = by_record[e["record"]["id"]]
            assert e["id"] == (s["put_event_id"] if e["op"] == "put" else s["retract_event_id"])
