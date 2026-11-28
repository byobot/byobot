"""The core's contract module agrees with the shared schema/v1 fixtures."""

import pytest

from byobot.core.contract import Contract, Rejected, mac
from conftest import newer_contract

from helpers import fixture

CASES = fixture("cases.json")["cases"]
IDS = fixture("ids.json")
CONTRACT = Contract.shipped()


def outcome(event: dict) -> str:
    try:
        CONTRACT.check(event)
    except Rejected:
        return "rejected"
    return "accepted" if CONTRACT.understands(event) else "stored_unprojected"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_fixture(case):
    assert outcome(case["data"]) == case["outcome"]
    if case.get("rule"):
        assert CONTRACT.broken_rules(case["data"]) == {case["rule"]}


def test_derived_ids_and_macs():
    key = bytes.fromhex(IDS["store_key"])
    for s in IDS["source_records"]:
        p = s["payload"]
        assert s["record_id"] == CONTRACT.derived("source_record", [p["source"], p["source_type"], p["source_id"]])
        assert s["mac"] == mac(key, p)
        assert s["put_event_id"] == CONTRACT.derived("source_revision", [s["record_id"], s["mac"]])
        assert s["retract_event_id"] == CONTRACT.derived("source_deletion", [s["record_id"], s["put_event_id"]])
        assert s["tombstone_event_id"] == CONTRACT.derived("source_deletion", [s["record_id"]])
    for o in IDS["overlays"]:
        assert o["record_id"] == CONTRACT.derived("overlay", [o["effect"], o["target"]["kind"], o["target"]["id"]])


def test_a_newer_contract_understands_newer_versions_and_kinds():
    newer = newer_contract(CONTRACT, 2, {"meal": {"version": 2, "description": "A meal."}})
    unknown = next(c["data"] for c in CASES if c["name"] == "unknown/kind-version")
    assert not CONTRACT.understands(unknown) and newer.understands(unknown)
    assert newer.understands({**unknown, "record": {"kind": "meal", "id": unknown["record"]["id"]}})


def test_library_activities_are_valid_activity_payloads(monkeypatch):
    monkeypatch.delenv("BYOBOT_LIBRARY")
    from byobot import library

    for a in library.activities():
        assert CONTRACT.payload_errors("activity", {k: v for k, v in a.items() if k != "id"}, a["id"]) == [], a["id"]


def test_payload_errors_names_what_is_wrong():
    assert CONTRACT.payload_errors("activity", {"name": "Plank"}, "plank") == []
    assert CONTRACT.payload_errors("activity", {}, "plank")
    assert CONTRACT.payload_errors("observation", {"code": "body_mass", "value": 80, "unit": "kg", "modality":
                                   "self_reported", "time": {"start": "2026-01-01T08:00:00Z"}}) == []


def test_a_day_that_does_not_exist_is_refused():
    assert CONTRACT.payload_errors("plan", {"name": "Base", "starts_on": "2026-02-30"})
    assert CONTRACT.payload_errors("plan", {"name": "Base", "starts_on": "2026-02-28"}) == []
