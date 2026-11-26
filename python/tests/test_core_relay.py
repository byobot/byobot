"""The rules every transport checks a relay device's events and samples with."""

import pytest

from byobot.core import hlc, relay
from byobot.util import ValidationFailed, uuid7

PHONE = "0193a0b0-0000-7000-8000-00000000ab01"
ME = {"kind": "human", "id": "me"}


def event(kind="effort", op="put", device_id=PHONE, actor=ME, **extra):
    payload = {"activity_id": "barbell_back_squat", "modality": "self_reported",
               "time": {"start": "2026-11-23T07:40:00-05:00"}} if op == "put" else {}
    return {"id": uuid7(), "schema_version": 1, "record": {"kind": kind, "id": uuid7()}, "op": op,
            "hlc": hlc.format((1764000000000, 0)), "ts": "2026-11-23T08:00:00-05:00", "device_id": device_id,
            "actor": actor, "prev": [], **({"kind_version": 1} if op == "put" else {}), "payload": payload, **extra}


def test_a_device_writes_only_what_its_person_writes(store):
    sent = [event(), event(device_id="another-device"), event(actor={"kind": "agent", "id": "coach"}),
            event(kind="source_record"), event(op="erase"), event(redacted_by=uuid7()), "not an event"]
    assert [r["status"] for r in relay.accept_events(store, PHONE, sent)] == ["accepted"] + ["rejected"] * 6


def test_a_push_is_counted_and_retrying_it_is_harmless(store):
    body = {"events": [event(), event(device_id="another-device")]}
    first = relay.push_events(store, PHONE, body)
    assert (first["accepted"], first["rejected"]) == (1, 1)
    again = relay.push_events(store, PHONE, body)
    assert (again["duplicate"], again["rejected"], again["accepted"]) == (1, 1, 0)


@pytest.mark.parametrize("body", [None, {}, {"events": {}}, {"events": [event()] * (relay.MAX_BATCH + 1)},
                                  {"events": [], "kinds": {"effort": 0}}])
def test_a_malformed_push_writes_nothing(store, body):
    with pytest.raises(ValidationFailed):
        relay.push_events(store, PHONE, body)
    assert store.position() == 0


def test_a_device_sends_only_apple_health_samples(store):
    sample = {"source": "apple_health", "source_id": "S1", "type": "HKQuantityTypeIdentifierBodyMass",
              "start": "2026-11-23T07:00:00Z", "value": 80.1, "unit": "kg"}
    assert relay.accept_samples(store, {"samples": [sample]})["written"] == 1
    with pytest.raises(ValidationFailed):
        relay.accept_samples(store, {"samples": [{**sample, "source": "whoop"}]})
    with pytest.raises(ValidationFailed):
        relay.accept_samples(store, {"samples": [sample] * (relay.MAX_BATCH + 1)})
