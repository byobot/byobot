"""The shared fixtures in schema/fixtures are the contract every implementation must pass."""

import pytest
from conftest import CASES

from byobot import contract


@pytest.mark.parametrize("c", CASES, ids=[c["name"] for c in CASES])
def test_shared_fixture(c):
    errors = contract.validate(c["schema"], c["data"])
    assert (not errors) == c["valid"], errors or "expected this case to be rejected"
    if c.get("rule"):
        assert {e.get("rule") for e in errors} == {c["rule"]}, errors


def test_fixtures_cover_every_event_type_and_telemetry_type():
    event_types = {c["data"]["type"] for c in CASES if c["schema"] == "event" and c["valid"]}
    assert event_types == set(contract.EVENT_TYPES)
    telemetry_types = {c["data"]["type"] for c in CASES if c["schema"] == "telemetry" and c["valid"]}
    assert telemetry_types == set(contract.schema("telemetry")["properties"]["type"]["enum"])


def test_every_case_has_a_description():
    assert all(c["description"] for c in CASES)


def test_payload_validation_matches_whole_event_validation():
    for c in CASES:
        if c["schema"] != "event" or not c["valid"]:
            continue
        assert contract.validate_payload(c["data"]["type"], c["data"]["payload"]) == []


def test_payload_validation_reports_paths():
    errors = contract.validate_payload("effort.performed", {"id": "x", "activity_id": "plank", "performed_at": "nope"})
    assert any(e["path"] == "$.performed_at" for e in errors)


def test_unknown_event_type_payload():
    assert contract.validate_payload("meal.logged", {}) != []


def test_event_type_enum_matches_python():
    assert contract.schema("event")["properties"]["type"]["enum"] == list(contract.EVENT_TYPES)


def test_semantic_rules_report_paths():
    errors = contract.validate_payload("effort.performed", {
        "id": "x", "activity_id": "run", "performed_at": "2026-11-20T08:00:00Z",
        "metrics": {"pace": {"min": 240, "max": 220, "unit": "s/km"}}})
    assert errors == [{"path": "$.metrics.pace", "message": "min is greater than max", "rule": "range-order"}]


def test_time_order_compares_across_offsets():
    ok = {"id": "s", "started_at": "2026-11-20T08:00:00-05:00", "ended_at": "2026-11-20T13:30:00Z"}
    assert contract.validate_payload("session.performed", ok) == []
    bad = {**ok, "ended_at": "2026-11-20T12:30:00Z"}
    assert [e["rule"] for e in contract.validate_payload("session.performed", bad)] == ["time-order"]


def test_every_convertible_unit_has_a_conversion():
    from byobot.store import STANDARD_UNITS

    registry = contract.schema("common")["$defs"]["metrics"]["properties"]
    for metric, conversions in STANDARD_UNITS.items():
        units = set(registry[metric]["properties"]["unit"]["enum"])
        assert set(conversions) <= units, metric
        assert units - set(conversions) <= {"%1rm"}, metric  # a share of 1RM has no kg value


def test_fixtures_cover_the_core_examples():
    names = {c["name"] for c in CASES}
    for name in ("event/session-planned-lift", "event/session-planned-intervals", "event/session-planned-stretch",
                 "event/session-planned-superset", "event/session-planned-emom", "event/effort-performed-climb",
                 "event/effort-performed-tennis", "event/unknown-metric", "event/bad-unit", "event/min-greater-than-max"):
        assert name in names
