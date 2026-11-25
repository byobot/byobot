"""The source lane on small hand-made histories: revisions, dependents, overlays and merged workouts."""

from datetime import datetime, timedelta, timezone

import pytest

from byobot.core import commands, sources
from byobot.core.store import Store
from byobot.util import ValidationFailed

ME = {"kind": "human", "id": "me"}


class Wall:
    def __init__(self):
        self.at = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db", device_id="phone", wall=Wall())
    yield s
    s.close()


def sleep(updated="2026-03-02T12:00:00Z", end="2026-03-02T12:30:00.000Z"):
    return {"id": "s1", "start": "2026-03-02T04:00:00.000Z", "end": end, "timezone_offset": "-05:00",
            "updated_at": updated, "score_state": "SCORED", "score": {"respiratory_rate": 15.2}}


def recovery():
    return {"cycle_id": 7, "sleep_id": "s1", "created_at": "2026-03-02T13:00:00.000Z",
            "updated_at": "2026-03-02T13:00:00Z", "score_state": "SCORED", "score": {"recovery_score": 61}}


def workout(wid="w1", start="2026-03-02T15:00:00.000Z", end="2026-03-02T16:00:00.000Z", hr=140):
    return {"id": wid, "start": start, "end": end, "timezone_offset": "-05:00", "sport_name": "running",
            "updated_at": "2026-03-02T17:00:00Z", "score_state": "SCORED",
            "score": {"average_heart_rate": hr, "distance_meter": 5000}}


def health(start="2026-03-02T15:05:00Z", end="2026-03-02T16:05:00Z"):
    return {"type": "HKWorkoutTypeIdentifier", "source_id": "h1", "value": "running", "start": start, "end": end,
            "raw": {"distance_m": 5100, "heart_rate_max_bpm": 170}}


def seen(store, code):
    return [r[0] for r in store.conn.execute("SELECT started_at FROM observations WHERE code = ?", (code,))]


def merged(store):
    return {code: (value, frm) for code, value, frm in store.conn.execute(
        "SELECT code, std_value, from_id FROM occurrence_metrics")}


def test_the_same_revision_twice_writes_once(store):
    assert len(sources.ingest(store, "whoop", [("sleep", "s1", sleep())])) == 1
    assert sources.ingest(store, "whoop", [("sleep", "s1", sleep())]) == []
    assert len(sources.ingest(store, "whoop", [("sleep", "s1", sleep("2026-03-02T14:00:00Z"))])) == 1
    assert len(sources.history(store, sources.record_id(store, "whoop", "sleep", "s1"))) == 2


def test_an_event_id_not_derived_from_its_payload_is_refused(store, tmp_path):
    [e] = sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    other = Store(tmp_path / "o.db", device_id="laptop", wall=Wall())
    forged = {**e, "payload": {**e["payload"], "raw": sleep("2026-03-02T15:00:00Z")}}
    assert other.accept(forged)[0] == "rejected"
    other.close()


def test_a_recovery_is_dated_when_its_sleep_ends_even_if_the_sleep_comes_later(store):
    sources.ingest(store, "whoop", [("recovery", "7", recovery())])
    assert seen(store, "recovery_score") == ["2026-03-02T13:00:00.000Z"]
    sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    assert seen(store, "recovery_score") == ["2026-03-02T07:30:00.000-05:00"]
    sources.ingest(store, "whoop", [("sleep", "s1", sleep("2026-03-02T14:00:00Z", "2026-03-02T12:45:00.000Z"))])
    assert seen(store, "recovery_score") == ["2026-03-02T07:45:00.000-05:00"]


def test_a_deletion_hides_the_record_and_a_new_revision_brings_it_back(store):
    sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    sources.ingest(store, "whoop", [], [("sleep", "s1")])
    assert seen(store, "respiratory_rate") == []
    sources.ingest(store, "whoop", [("sleep", "s1", sleep("2026-03-02T14:00:00Z"))])
    assert len(seen(store, "respiratory_rate")) == 1


def test_sources_fill_each_others_gaps_and_the_better_one_wins(store):
    sources.ingest(store, "apple_health", [("workout", "h1", health())])
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    h1 = sources.record_id(store, "apple_health", "workout", "h1")
    got = merged(store)
    assert got["distance"] == (5000, w1) and got["heart_rate_max"] == (170, h1)
    assert store.conn.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 1
    prefer = {"default": ["whoop"], "codes": {"distance": ["apple_health", "whoop"]}}
    commands.log(store, ME, [{"kind": "setting", "id": "source_priority", "payload": {"value": prefer}}])
    assert merged(store)["distance"] == (5100, h1) and merged(store)["heart_rate"] == (140, w1)


def test_workouts_overlapping_less_than_half_stay_apart(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    sources.ingest(store, "apple_health", [("workout", "h1", health("2026-03-02T15:50:00Z", "2026-03-02T16:50:00Z"))])
    assert store.conn.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 2


def test_a_correction_outlives_a_rescore_and_exclude_hides_the_record(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    commands.correct(store, ME, w1, {"metrics": {"heart_rate": {"value": 150, "unit": "/min"}}})
    sources.ingest(store, "whoop", [("workout", "w1", {**workout(hr=120), "updated_at": "2026-03-03T00:00:00Z"})])
    assert merged(store)["heart_rate"] == (150, w1)
    commands.exclude(store, ME, w1)
    assert store.conn.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 0


def test_a_correction_must_use_the_registry(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    with pytest.raises(ValidationFailed):
        commands.correct(store, ME, w1, {"metrics": {"heart_rate": {"value": 150, "unit": "kg"}}})


def test_a_rebuild_reads_the_same_as_writing_incrementally(store):
    sources.ingest(store, "whoop", [("recovery", "7", recovery()), ("workout", "w1", workout())])
    sources.ingest(store, "apple_health", [("workout", "h1", health())])
    sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    tables = ("observations", "sessions", "source_sessions", "metrics", "occurrences", "occurrence_items",
              "occurrence_metrics")
    before = {t: sorted(map(tuple, store.conn.execute(f"SELECT * FROM {t}"))) for t in tables}
    store.rebuild()
    assert {t: sorted(map(tuple, store.conn.execute(f"SELECT * FROM {t}"))) for t in tables} == before
