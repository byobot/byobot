"""The rules every transport checks a relay device's events and samples with."""

import pytest

from byobot.core import commands, hlc, relay
from byobot.core.contract import sync_errors
from byobot.core.store import Store
from byobot.util import ValidationFailed, uuid7

PHONE = "0193a0b0-0000-7000-8000-00000000ab01"
ME = {"kind": "human", "id": "me"}


def event(kind="effort", op="put", device_id=PHONE, actor=ME, **extra):
    payload = {"activity_id": "barbell_back_squat",
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


def logged(n, start=1764000000000):
    """n sets as a phone logs them, one a second."""
    return [{**event(), "hlc": hlc.format((start + 1000 * i, 0))} for i in range(n)]


def test_the_plan_carries_the_store_id_hub_clock_and_kind_versions(store):
    relay.accept_events(store, PHONE, logged(1, start=1795000000000))
    window = relay.plan_window(store, "2026-11-22", "2026-12-05", PHONE)
    assert sync_errors("plan_window", window) == []
    assert window["store_id"] == store.store_id
    assert window["hlc"] == store.hlc == hlc.format((1795000000000, 0))
    assert window["kind_versions"]["effort"] == store.contract.version("effort")


def test_the_plan_names_what_was_erased(store):
    [sent] = logged(1)
    relay.accept_events(store, PHONE, [sent])
    commands.erase(store, ME, "effort", sent["record"]["id"])
    window = relay.plan_window(store, "2026-11-22", "2026-12-05", PHONE)
    assert window["erased"] == [{"kind": "effort", "id": sent["record"]["id"]}]


def test_the_plan_tells_a_device_what_the_hub_holds_and_refused(store):
    sent = logged(2) + [{**event(device_id="another-device")}]
    relay.accept_events(store, PHONE, sent)
    window = relay.plan_window(store, "2026-11-22", "2026-12-05", PHONE)
    assert window["received"] == {PHONE: sent[1]["hlc"]}
    assert window["rejected"] == [{"id": sent[2]["id"], "reason": relay.accept_events(store, PHONE, sent[2:])[0]
                                   ["reason"]}]
    assert relay.plan_window(store, "2026-11-22", "2026-12-05", "a-new-phone")["received"] == {}
    assert relay.plan_window(store, "2026-11-22", "2026-12-05", "a-new-phone")["rejected"] == []


def test_a_hub_restored_from_an_older_backup_ends_complete(store, tmp_path):
    sent = logged(3)
    relay.accept_events(store, PHONE, sent[:2])
    backup = list(store.export())
    relay.accept_events(store, PHONE, sent[2:])
    with Store(tmp_path / "restored.db") as restored:
        restored.restore(backup)
        marker = relay.plan_window(restored, "2026-11-22", "2026-12-05", PHONE)["received"][PHONE]
        again = [e for e in sent if e["hlc"] > marker]  # what the phone sends again
        assert [r["status"] for r in relay.accept_events(restored, PHONE, again)] == ["accepted"]
        assert {e["id"] for e in restored.events()} == {e["id"] for e in store.events()}


def test_a_device_s_refusals_stay_bounded_and_go_when_it_is_unpaired(store, monkeypatch):
    monkeypatch.setattr("byobot.core.store.MAX_REJECTIONS", 3)
    store.pair(PHONE, "Example iPhone", "ab" * 32)
    for _ in range(2):
        relay.accept_events(store, PHONE, [event(device_id="another-device") for _ in range(2)])
    assert len(store.rejections(PHONE, limit=10)) == 3
    store.unpair(PHONE)
    assert store.rejections(PHONE) == []


def test_a_setting_is_never_erased_so_it_can_always_be_set_again(store):
    [put] = commands.log(store, ME, [{"kind": "setting", "id": "equipment", "payload": {"value": ["barbell"]}}])
    with pytest.raises(ValidationFailed, match="remove it instead"):
        commands.erase(store, ME, "setting", "equipment")
    commands.remove(store, ME, "setting", "equipment")
    later = {**event(kind="setting"), "record": {"kind": "setting", "id": "equipment"}, "payload": {"value": ["rack"]},
             "hlc": hlc.format((1795000000000, 0)), "prev": store.record("setting", "equipment")["heads"]}
    assert relay.accept_events(store, PHONE, [later])[0]["status"] == "accepted"
    assert store.get("setting", "equipment")["payload"] == {"value": ["rack"]}
    assert relay.plan_window(store, "2026-11-22", "2026-12-05", PHONE)["erased"] == []
