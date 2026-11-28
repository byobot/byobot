"""The source lane read through the store's own tables: observation ids, dating, sample shapes, merges and refusals.

Behaviour any driver can show (revisions, merges, corrections over a rescore) is in core/test_sources.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from byobot.core import commands, relay, sources
from byobot.core.store import Store
from byobot.util import ValidationFailed

from helpers import ME, Wall, open_store, whoop_workout

NOON = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "s.db", "phone", Wall(NOON))
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
    assert sources.ingest(store, "whoop", [("sleep", "s1", sleep("2026-03-02T14:00:00Z"))]) == []
    rescored = {**sleep("2026-03-02T15:00:00Z"), "score": {"respiratory_rate": 15.9}}
    assert len(sources.ingest(store, "whoop", [("sleep", "s1", rescored)])) == 1
    assert len(sources.history(store, sources.record_id(store, "whoop", "sleep", "s1"))) == 2


def test_an_event_id_not_derived_from_its_payload_is_refused(store, tmp_path):
    [e] = sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    other = open_store(tmp_path / "o.db", wall=Wall(NOON))
    forged = {**e, "payload": {**e["payload"], "raw": sleep("2026-03-02T15:00:00Z")}}
    assert other.accept(forged)[0] == "rejected"
    other.close()


def test_a_recovery_is_dated_when_its_sleep_ends_even_if_the_sleep_comes_later(store):
    sources.ingest(store, "whoop", [("recovery", "7", recovery())])
    assert seen(store, "whoop_recovery_score") == ["2026-03-02T13:00:00.000Z"]
    sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    assert seen(store, "whoop_recovery_score") == ["2026-03-02T07:30:00.000-05:00"]
    sources.ingest(store, "whoop", [("sleep", "s1", sleep("2026-03-02T14:00:00Z", "2026-03-02T12:45:00.000Z"))])
    assert seen(store, "whoop_recovery_score") == ["2026-03-02T07:45:00.000-05:00"]


def test_an_observation_id_is_its_record_and_code_so_a_new_field_moves_none(store):
    sources.ingest(store, "whoop", [("recovery", "7", recovery())])
    rid = sources.record_id(store, "whoop", "recovery", "7")
    ids = lambda: dict(store.conn.execute("SELECT code, id FROM observations"))
    assert ids() == {"whoop_recovery_score": f"{rid}:whoop_recovery_score"}
    more = {**recovery(), "updated_at": "2026-03-02T14:00:00Z", "score": {"resting_heart_rate": 52, "recovery_score": 61}}
    sources.ingest(store, "whoop", [("recovery", "7", more)])
    assert ids() == {"whoop_recovery_score": f"{rid}:whoop_recovery_score", "resting_heart_rate": f"{rid}:resting_heart_rate"}


def test_a_deletion_hides_the_record_and_a_new_revision_brings_it_back(store):
    sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    sources.ingest(store, "whoop", [], [("sleep", "s1")])
    assert seen(store, "respiratory_rate") == []
    sources.ingest(store, "whoop", [("sleep", "s1", sleep("2026-03-02T14:00:00Z"))])
    assert len(seen(store, "respiratory_rate")) == 1


def test_a_deletion_that_comes_before_its_sample_keeps_it_from_ever_showing(store):
    sample = {"source": "apple_health", "source_id": "h1", "type": "HKQuantityTypeIdentifierBodyMass",
              "start": "2026-03-02T07:00:00Z", "end": "2026-03-02T07:00:00Z", "value": 80.1, "unit": "kg"}
    gone = {"source": "apple_health", "source_id": "h1", "type": sample["type"]}
    assert len(sources.ingest_samples(store, [], [gone])["apple_health"]) == 1
    assert sources.ingest_samples(store, [], [gone]) == {"apple_health": []}
    assert sources.ingest_samples(store, [sample]) == {"apple_health": []}
    store.rebuild()
    assert seen(store, "body_mass") == []


def test_a_sample_keeps_its_origin_app_and_drops_fields_it_doesnt_know(store):
    sample = {"source": "apple_health", "source_id": "h2", "type": "HKQuantityTypeIdentifierBodyMass",
              "start": "2026-03-02T07:00:00Z", "value": 80.1, "unit": "kg", "origin_app": "com.example.scale"}
    [put] = sources.ingest_samples(store, [sample])["apple_health"]
    assert put["payload"]["raw"]["origin_app"] == "com.example.scale"
    assert sources.ingest_samples(store, [{**sample, "from_a_newer_phone": 1}]) == {"apple_health": []}


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


def test_a_correction_must_use_the_registry(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    with pytest.raises(ValidationFailed):
        commands.correct(store, ME, w1, {"metrics": {"heart_rate": {"value": 150, "unit": "kg"}}})


def test_a_corrected_start_past_the_source_end_is_refused(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    with pytest.raises(ValidationFailed):
        commands.correct(store, ME, w1, {"time": {"start": "2026-03-02T17:00:00Z"}})


def test_taking_back_a_metric_that_isnt_corrected_writes_nothing(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    with pytest.raises(ValidationFailed):
        commands.correct(store, ME, w1, {"metrics": {"heart_rate": None}})
    commands.correct(store, ME, w1, {"metrics": {"heart_rate": {"value": 150, "unit": "/min"}}})
    with pytest.raises(ValidationFailed):
        commands.correct(store, ME, w1, {"metrics": {"heart_rate": None}})
    assert merged(store)["heart_rate"] == (150, w1)


def test_a_rebuild_reads_the_same_as_writing_incrementally(store):
    sources.ingest(store, "whoop", [("recovery", "7", recovery()), ("workout", "w1", workout())])
    sources.ingest(store, "apple_health", [("workout", "h1", health())])
    sources.ingest(store, "whoop", [("sleep", "s1", sleep())])
    tables = ("observations", "sessions", "source_sessions", "metrics", "occurrences", "occurrence_items",
              "occurrence_metrics")
    before = {t: sorted(map(tuple, store.conn.execute(f"SELECT * FROM {t}"))) for t in tables}
    store.rebuild()
    assert {t: sorted(map(tuple, store.conn.execute(f"SELECT * FROM {t}"))) for t in tables} == before


def test_apple_health_workouts_read_the_totals_the_phone_sends(store):
    w = {**health(), "value": "traditionalStrengthTraining",
         "raw": {"duration_s": 3000, "energy_kcal": 410.5, "heart_rate_avg_bpm": 128}}
    sources.ingest(store, "apple_health", [("workout", "h1", w)])
    name, = store.conn.execute("SELECT name FROM source_sessions").fetchone()
    got = merged(store)
    assert name == "strength training" and got["duration"][0] == 3000 and got["active_energy"][0] == 410.5


def test_a_running_total_sent_again_with_a_newer_stamp_writes_nothing(store):
    day = {"type": "HKQuantityTypeIdentifierStepCount", "source_id": "daily:steps:2026-03-02", "value": 4000,
           "unit": "count", "start": "2026-03-02T05:00:00Z", "end": "2026-03-03T05:00:00Z",
           "raw": {"modified_at": "2026-03-02T12:00:00Z"}}
    key = ("quantity_sample", day["source_id"])
    assert len(sources.ingest(store, "apple_health", [(*key, day)])) == 1
    assert sources.ingest(store, "apple_health", [(*key, {**day, "raw": {"modified_at": "2026-03-02T13:00:00Z"}})]) == []
    later = {**day, "value": 6500, "raw": {"modified_at": "2026-03-02T18:00:00Z"}}
    assert len(sources.ingest(store, "apple_health", [(*key, later)])) == 1
    assert [r[0] for r in store.conn.execute("SELECT value FROM observations")] == [6500]


def test_revision_times_compare_as_instants(store):
    """13:00+02:00 is earlier than 12:00Z, though it sorts later as text."""
    rid = sources.record_id(store, "whoop", "sleep", "s1")
    sources.ingest(store, "whoop", [("sleep", "s1", {**sleep("2026-03-02T13:00:00+02:00"), "score": {"respiratory_rate": 1}})])
    sources.ingest(store, "whoop", [("sleep", "s1", {**sleep("2026-03-02T12:00:00Z"), "score": {"respiratory_rate": 2}})])
    assert store.get("source_record", rid)["payload"]["raw"]["score"]["respiratory_rate"] == 2


def test_without_revision_times_the_last_written_is_current(store):
    def weight(kg):
        return {"source": "apple_health", "source_id": "w1", "type": "HKQuantityTypeIdentifierBodyMass",
                "start": "2026-03-02T07:00:00Z", "end": "2026-03-02T07:00:00Z", "value": kg, "unit": "kg"}

    for kg in (80.1, 80.2, 80.3, 80.4):
        sources.ingest_samples(store, [weight(kg)])
        assert seen_value(store, "body_mass") == kg


def test_a_changed_sample_without_a_revision_time_is_current_even_when_the_clock_went_back(store):
    def weight(kg):
        return {"source": "apple_health", "source_id": "w1", "type": "HKQuantityTypeIdentifierBodyMass",
                "start": "2026-03-02T07:00:00Z", "end": "2026-03-02T07:00:00Z", "value": kg, "unit": "kg"}

    sources.ingest_samples(store, [weight(80.1)])
    sources.ingest_samples(store, [weight(80.2)])
    store.wall.at -= timedelta(hours=1)
    sources.ingest_samples(store, [weight(80.3)])
    assert seen_value(store, "body_mass") == 80.3
    assert sources.ingest_samples(store, [weight(80.3)]) == {"apple_health": []}  # the same content again writes nothing


def seen_value(store, code):
    [v] = [r[0] for r in store.conn.execute("SELECT value FROM observations WHERE code = ?", (code,))]
    return v


def test_an_exclude_can_be_undone_and_a_correction_removed(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    excluded = commands.exclude(store, ME, w1)
    assert store.conn.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 0
    commands.undo(store, ME, excluded["id"])
    assert store.conn.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 1
    fixed = commands.correct(store, ME, w1, {"metrics": {"heart_rate": {"value": 150, "unit": "/min"}}})
    commands.remove(store, ME, "overlay", fixed["record"]["id"])
    assert merged(store)["heart_rate"] == (140, w1)


def test_a_correction_can_move_a_workout(store):
    sources.ingest(store, "whoop", [("workout", "w1", workout())])
    w1 = sources.record_id(store, "whoop", "workout", "w1")
    fixed = commands.correct(store, ME, w1, {"time": {"start": "2026-03-02T09:30:00", "end": "2026-03-02T10:30:00"}})
    assert "modality" not in fixed["payload"]["fields"] and fixed["payload"]["fields"]["time"]["start"][19:]


WORKOUT = "00000000-0000-4000-8000-0000000000c1"


def log_session(store, name, start, end):
    [e] = commands.log(store, ME, [{"kind": "session",
                                    "payload": {"name": name, "time": {"start": start, "end": end}}}])
    return e["record"]["id"]


def test_two_of_your_sessions_stay_two_workouts_after_a_same_as(store):
    strength = log_session(store, "Strength", "2026-11-20T09:00:00Z", "2026-11-20T10:00:00Z")
    conditioning = log_session(store, "Conditioning", "2026-11-20T10:00:00Z", "2026-11-20T10:30:00Z")
    # WHOOP recorded both as one continuous workout.
    sources.ingest(store, "whoop", [("workout", WORKOUT, {
        "id": WORKOUT, "start": "2026-11-20T09:00:00.000Z", "end": "2026-11-20T10:30:00.000Z",
        "timezone_offset": "+00:00", "sport_name": "functional-fitness", "updated_at": "2026-11-20T11:00:00.000Z",
        "score_state": "SCORED", "score": {"average_heart_rate": 140}})])
    rid = sources.record_id(store, "whoop", "workout", WORKOUT)
    assert store.rows("SELECT count(*) AS n FROM occurrences")[0]["n"] == 2
    # You say the WHOOP workout is your conditioning session.
    commands.same_as(store, ME, rid, "session", conditioning)
    homes = {r["id"]: r["occurrence_id"] for r in store.rows("SELECT id, occurrence_id FROM occurrence_items")}
    assert homes[strength] != homes[conditioning], homes


def _weight(source_id, value):
    return {"source": "apple_health", "source_id": source_id, "type": "HKQuantityTypeIdentifierBodyMass",
            "value": value, "unit": "kg", "start": "2026-03-02T07:00:00-05:00", "end": "2026-03-02T07:00:00-05:00"}


@pytest.mark.parametrize("odd", [{"kg": 80.1, "precision": 0.1}, [80.1, 80.2], 10 ** 20],
                         ids=["object", "list", "beyond_64_bits"])
def test_a_sample_whose_value_is_not_a_number_does_not_crash_the_whole_samples_push(tmp_path, odd):
    """A sample a newer app shapes differently reaches the reducer, which can't bind the value. A list or an object
    raises sqlite3.ProgrammingError, which the reducer guard doesn't catch, so the push fails with a traceback every
    time the phone retries it; a number past 64 bits raises OverflowError, so the whole batch is refused. A source
    record keeps its raw as given and reads as nothing where its mapper can't read it, as one with no mapper does."""
    with open_store(tmp_path / "a.db") as s:
        relay.accept_samples(s, {"samples": [_weight("good-1", 80.0), _weight("odd-1", odd)], "deleted": []})
        rows = s.rows("SELECT value FROM observations WHERE code = 'body_mass'")
        held = s.rows("SELECT count(*) AS n FROM events")[0]["n"]
    assert held == 2 and {"value": 80.0} in rows, "the raw sample should be kept, as one this build can't read"


def test_correcting_only_the_start_of_an_imported_workout_keeps_its_end(tmp_path):
    """`correct` says it merges into earlier corrections, but the reducer takes a corrected `time` whole: a start
    alone turns the workout into an instant, so it no longer overlaps (or merges with) the session it was moved
    to line up with."""
    with open_store(tmp_path / "a.db") as s:
        [ev] = sources.ingest(s, "whoop", [("workout", "2001", whoop_workout(
            "2001", "2026-03-02T14:00:00.000Z", "2026-03-02T15:00:00.000Z"))])
        rid = ev["record"]["id"]
        commands.correct(s, ME, rid, {"time": {"start": "2026-03-02T08:55:00-05:00"}})
        [row] = s.rows("SELECT started_at, ended_at FROM source_sessions WHERE id = ?", (rid,))
    assert row["started_at"] == "2026-03-02T08:55:00-05:00"
    assert row["ended_at"] == "2026-03-02T10:00:00.000-05:00", "the source's end was dropped by a start-only fix"


def test_a_sample_without_an_end_reads_as_a_point(tmp_path):
    with Store(tmp_path / "s.db") as store:
        sample = {"source": "apple_health", "source_id": "a1", "type": "HKQuantityTypeIdentifierBodyMass",
                  "start": "2026-03-02T07:00:00.000-05:00", "value": 80.0, "unit": "kg"}
        relay.accept_samples(store, {"samples": [sample]})
        assert store.rows("SELECT code, std_value FROM observations") == [{"code": "body_mass", "std_value": 80.0}]
