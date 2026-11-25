"""The v0 driver round-trips intents into today's store and reads them back canonically."""

import pytest

from .driver import Unsupported
from .drivers.v0 import V0
from .snapshot import diff, equal

HUMAN = {"kind": "human", "id": "me"}
T0 = "2026-03-02T10:00:00.000Z"


@pytest.fixture
def v0(tmp_path):
    driver = V0()
    store = driver.open("laptop", tmp_path)
    yield driver, store
    driver.close(store)


def seed(driver, store):
    driver.author(store, {"intent": "define_activity", "record": {"id": "row", "name": "Row"}}, T0, HUMAN)
    driver.author(store, {"intent": "log_session", "record": {
        "id": "s1", "started_at": T0, "efforts": [
            {"id": "e1", "activity_id": "row", "performed_at": T0, "metrics": {"reps": {"value": 5}}}]}},
        T0, HUMAN)


def test_snapshot_shows_authored_records(v0):
    driver, store = v0
    seed(driver, store)
    records = driver.snapshot(store)["records"]
    assert records["effort/e1"] == {
        "fields": {"activity_id": "row", "performed_at": T0, "session_id": "s1", "metrics": {"reps": {"value": 5}}},
        "by": "human:me", "on": "laptop"}
    assert records["session/s1"]["fields"] == {"started_at": T0}


def test_events_deliver_to_another_replica(v0, tmp_path):
    driver, store = v0
    seed(driver, store)
    events = [e for e in driver.log_events(store)]
    other = driver.open("phone", tmp_path)
    assert [o for o, _ in driver.deliver(other, events)] == ["accepted"] * len(events)
    assert equal(driver.snapshot(other), driver.snapshot(store)), diff(driver.snapshot(other), driver.snapshot(store))
    driver.close(other)


def test_rebuild_matches_incremental_state(v0):
    driver, store = v0
    seed(driver, store)
    driver.author(store, {"intent": "edit", "record": ["effort", "e1"], "fields": {"metrics": {"reps": {"value": 9}}}},
                  "2026-03-02T10:05:00.000Z", HUMAN)
    before = driver.snapshot(store)
    driver.rebuild(store)
    assert equal(before, driver.snapshot(store))


def test_unexpressible_operations_are_unsupported(v0):
    driver, store = v0
    with pytest.raises(Unsupported):
        driver.export_log(store)
    with pytest.raises(Unsupported):
        driver.author(store, {"intent": "erase", "record": ["effort", "e1"]}, T0, HUMAN)
