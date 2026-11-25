import json
import sqlite3

import pytest
from conftest import case

from byobot.store import Store, query
from byobot.util import Conflict, NotFound, ValidationFailed

ME = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "claude-code", "model": "example-model-1"}
WATCH = {"kind": "connector", "id": "garmin"}


def payload(name):
    return case(name)["payload"]


def projection(store):
    tables = ["activities", "plans", "planned_sessions", "planned_blocks", "planned_efforts", "performed_sessions",
              "performed_efforts", "metrics", "undone_events", "telemetry_samples"]
    return {t: store.rows(f"SELECT * FROM {t} ORDER BY 1, 2") for t in tables}


def seed(store):
    """A planned lift, and round 2 of its main block logged against it."""
    store.append("activity.defined", payload("event/activity-defined"), ME)
    store.append("plan.defined", payload("event/plan-defined-by-agent"), AGENT)
    store.append("session.planned", payload("event/session-planned-lift"), AGENT)
    return store.append("effort.performed", payload("event/effort-performed-lift"), ME)


def test_token_hash_comparison_checks_every_live_peer(store, monkeypatch):
    import secrets

    from byobot import store as store_mod
    from byobot.serve import token_hash

    calls = []
    real = secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(store_mod.secrets, "compare_digest", spy)
    alpha, beta = token_hash("alpha-token-value"), token_hash("beta-token-value")
    store.pair_peer("example-iphone", "Example iPhone", alpha)
    store.pair_peer("example-laptop", "example-laptop", beta)
    calls.clear()
    found = store.peer_for_token(alpha)
    assert found["device_id"] == "example-iphone" and found["name"] == "Example iPhone"
    assert sorted(a for a, _ in calls) == sorted([alpha, beta])  # a match does not stop the scan
    calls.clear()
    assert store.peer_for_token(token_hash("no-such-token")) is None
    assert sorted(a for a, _ in calls) == sorted([alpha, beta])
    store.unpair_peer("example-iphone")
    calls.clear()
    assert store.peer_for_token(alpha) is None
    assert [a for a, _ in calls] == [beta]
    assert store.peer_for_token(beta)["device_id"] == "example-laptop"


def test_init_creates_store_with_device_id(store, byobot_home):
    assert (byobot_home / "byobot.db").exists()
    assert len(store.device_id) == 36
    again = Store(byobot_home / "byobot.db")
    assert again.device_id == store.device_id  # the device id survives reopening
    again.close()


def test_store_lives_under_byobot_home(byobot_home):
    from byobot.util import db_path

    assert db_path() == byobot_home / "byobot.db"


def test_append_records_provenance(store):
    seed(store)
    plan = store.rows("SELECT * FROM plans")[0]
    assert (plan["actor_kind"], plan["actor_id"], plan["actor_model"]) == ("agent", "claude-code", "example-model-1")
    assert plan["device_id"] == store.device_id
    event = store.rows("SELECT * FROM events WHERE id = ?", [plan["event_id"]])[0]
    assert event["type"] == "plan.defined"
    assert json.loads(event["envelope"])["actor"] == AGENT
    metric = store.rows("SELECT event_id FROM metrics WHERE owner_type = 'performed_effort'")[0]
    assert store.rows("SELECT type FROM events WHERE id = ?", [metric["event_id"]]) == [{"type": "effort.performed"}]


def test_planned_session_is_blocks_and_efforts_in_order(store):
    seed(store)
    blocks = store.rows("SELECT id, position, rounds FROM planned_blocks ORDER BY position")
    assert blocks == [{"id": "b-warmup", "position": 1, "rounds": 1}, {"id": "b-main", "position": 2, "rounds": 3},
                      {"id": "b-amrap", "position": 3, "rounds": 1}]
    assert store.metrics_of("planned_block", "b-main") == {"rest": {"value": 3, "unit": "min"}}
    assert store.metrics_of("planned_effort", "e-squat")["reps"] == {"min": 3, "max": 5}


def test_metrics_keep_what_was_written_and_a_standard_unit(store):
    store.append("session.planned", payload("event/session-planned-intervals"), AGENT)
    row = store.rows("SELECT * FROM metrics WHERE owner_id = 'b-easy' OR owner_id = 'e-easy' AND metric = 'duration'")[0]
    assert (row["value"], row["unit"], row["std_value"], row["std_unit"]) == (15, "min", 900, "s")
    pace = store.rows("SELECT min, max, std_min, std_max, std_unit FROM metrics WHERE metric = 'pace'")[0]
    assert pace == {"min": 215, "max": 225, "std_min": 215, "std_max": 225, "std_unit": "s/km"}
    hr = store.rows("SELECT value, min, max FROM metrics WHERE metric = 'heart_rate'")[0]
    assert hr == {"value": None, "min": None, "max": 145}


def test_unit_conversions(store):
    store.append("effort.performed", {"id": "x1", "activity_id": "run", "performed_at": "2026-11-20T08:00:00Z", "metrics": {
        "distance": {"value": 1, "unit": "mi"}, "pace": {"value": 2, "unit": "s/500m"}, "load": {"value": 10, "unit": "lb"},
        "x.grade": {"value": "V4"}, "x.sent": {"value": True}, "x.speed": {"value": 3, "unit": "m/s"}}}, ME)
    got = {r["metric"]: r for r in store.rows("SELECT * FROM metrics")}
    assert got["distance"]["std_value"] == pytest.approx(1609.344)
    assert got["pace"]["std_value"] == 4
    assert got["load"]["std_value"] == pytest.approx(4.5359, abs=1e-4) and got["load"]["std_unit"] == "kg"
    assert (got["x.grade"]["value_text"], got["x.grade"]["value"]) == ("V4", None)
    assert got["x.sent"]["value"] == 1  # true is stored as 1
    assert (got["x.speed"]["std_value"], got["x.speed"]["std_unit"]) == (3, "m/s")  # unknown unit: kept as written


def test_plan_vs_actual(store):
    seed(store)
    rows = store.rows("SELECT * FROM plan_vs_actual ORDER BY block_position, effort_position")
    assert [r["planned_effort_id"] for r in rows] == ["e-warm", "e-squat", "e-amrap"]
    squat = rows[1]
    assert (squat["rounds"], squat["round"], squat["performed_effort_id"]) == (3, 2, "eff-1")
    assert (squat["target_reps_min"], squat["target_reps_max"], squat["actual_reps"]) == (3, 5, 5)
    assert (squat["target_load_kg_min"], squat["actual_load_kg"]) == (100, 100)
    assert (squat["target_rpe_min"], squat["actual_rpe"]) == (8, 8.5)
    amrap = rows[2]
    assert (amrap["target_reps_min"], amrap["target_reps_max"]) == (8, None)  # at least 8
    assert amrap["target_load_kg_min"] is None  # 70% of 1RM has no kg value
    assert rows[0]["performed_effort_id"] is None


def test_strength_sets(store):
    seed(store)
    store.append("effort.performed", payload("event/effort-performed-tennis"), WATCH)
    store.append("effort.performed", {**payload("event/effort-performed-lift"), "id": "eff-lb",
                                      "metrics": {"reps": {"value": 8}, "load": {"value": 185, "unit": "lb"}}}, ME)
    rows = {r["effort_id"]: r for r in store.rows("SELECT * FROM strength_sets")}
    assert set(rows) == {"eff-1", "eff-lb"}  # tennis has no reps or load
    assert (rows["eff-1"]["reps"], rows["eff-1"]["load_kg"], rows["eff-1"]["rpe"]) == (5, 100, 8.5)
    assert rows["eff-lb"]["load_kg"] == pytest.approx(83.9146, abs=1e-3) and rows["eff-lb"]["load_unit"] == "lb"


def test_session_performed_with_inline_efforts(store):
    store.append("session.performed", payload("event/session-performed-watch"), WATCH)
    efforts = store.rows("SELECT id, session_id, performed_at FROM performed_efforts ORDER BY id")
    assert efforts == [
        {"id": "w-1", "session_id": "done-track", "performed_at": "2026-11-25T06:46:00-05:00"},
        {"id": "w-2", "session_id": "done-track", "performed_at": "2026-11-25T06:30:00-05:00"},  # session start
    ]
    assert store.metrics_of("performed_session", "done-track")["distance"] == {"value": 6.1, "unit": "km"}


def test_relogging_a_session_replaces_its_inline_efforts_only(store):
    watch = payload("event/session-performed-watch")
    store.append("session.performed", watch, WATCH)
    store.append("effort.performed", {"id": "extra", "session_id": "done-track", "activity_id": "plank",
                                      "performed_at": "2026-11-25T07:20:00-05:00"}, ME)
    store.append("session.performed", {**watch, "efforts": watch["efforts"][:1]}, WATCH)
    assert [r["id"] for r in store.rows("SELECT id FROM performed_efforts ORDER BY id")] == ["extra", "w-1"]
    assert store.rows("SELECT owner_id FROM metrics WHERE owner_id = 'w-2'") == []


def test_replanning_a_session_replaces_its_blocks(store):
    lift = payload("event/session-planned-lift")
    store.append("session.planned", lift, AGENT)
    store.append("session.planned", {**lift, "blocks": lift["blocks"][:1]}, AGENT)
    assert [r["id"] for r in store.rows("SELECT id FROM planned_blocks")] == ["b-warmup"]
    assert [r["id"] for r in store.rows("SELECT id FROM planned_efforts")] == ["e-warm"]
    assert store.rows("SELECT DISTINCT owner_id FROM metrics ORDER BY 1") == [{"owner_id": "e-warm"}]


def test_events_are_append_only(store):
    seed(store)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("UPDATE events SET type = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.conn.execute("DELETE FROM events")


def test_invalid_event_is_rejected_and_nothing_is_written(store):
    with pytest.raises(ValidationFailed) as err:
        store.append("effort.performed", {"id": "s", "activity_id": "plank", "performed_at": "2026-11-20T08:00:00Z",
                                          "metrics": {"rep": {"value": 5}}}, ME)
    assert err.value.details
    assert store.counts()["events"] == 0


def test_semantic_rule_rejects_event(store):
    bad = {"id": "s", "activity_id": "run", "performed_at": "2026-11-20T08:00:00Z",
           "metrics": {"distance": {"min": 5, "max": 3, "unit": "km"}}}
    with pytest.raises(ValidationFailed) as err:
        store.append("effort.performed", bad, ME)
    assert err.value.details[0]["rule"] == "range-order"


def test_duplicate_event_id_conflicts(store):
    event = case("event/activity-defined")
    store.accept(event)
    with pytest.raises(Conflict):
        store.accept(event)


def test_undo_and_undo_of_undo(store):
    performed = seed(store)
    undo = store.append("event.undone", {"event_id": performed["id"], "reason": "wrong weight"}, ME)
    assert store.rows("SELECT * FROM performed_efforts") == []
    assert store.rows("SELECT * FROM metrics WHERE owner_type = 'performed_effort'") == []
    assert store.rows("SELECT * FROM undone_events") == [{"event_id": performed["id"], "undo_event_id": undo["id"]}]
    assert store.counts()["events"] == 5  # nothing was deleted

    store.append("event.undone", {"event_id": undo["id"]}, ME)
    assert [r["id"] for r in store.rows("SELECT * FROM performed_efforts")] == ["eff-1"]
    assert store.metrics_of("performed_effort", "eff-1")["reps"] == {"value": 5}


def test_undo_restores_previous_definition(store):
    store.append("activity.defined", {"id": "plank", "name": "Plank"}, ME)
    second = store.append("activity.defined", {"id": "plank", "name": "Front plank"}, ME)
    assert store.rows("SELECT name FROM activities") == [{"name": "Front plank"}]
    store.append("event.undone", {"event_id": second["id"]}, ME)
    assert store.rows("SELECT name FROM activities") == [{"name": "Plank"}]


def test_undo_errors(store):
    performed = seed(store)
    with pytest.raises(NotFound):
        store.append("event.undone", {"event_id": "0193a0b0-0000-7000-8000-999999999999"}, ME)
    store.append("event.undone", {"event_id": performed["id"]}, ME)
    with pytest.raises(Conflict):
        store.append("event.undone", {"event_id": performed["id"]}, ME)


def test_rebuild_reproduces_every_derived_table(store):
    performed = seed(store)
    store.append("session.planned", payload("event/session-planned-intervals"), AGENT)
    store.append("session.performed", payload("event/session-performed-watch"), WATCH)
    store.append("event.undone", {"event_id": performed["id"]}, ME)
    store.ingest([case("telemetry/body-mass"), case("telemetry/sleep-stage")])
    before = projection(store)
    result = store.rebuild()
    assert result["ledger"] == {"events": 7, "applied": 5, "undone": 1}
    assert result["telemetry"] == {"raw_records": 2}
    assert projection(store) == before


def test_every_valid_fixture_event_applies(store):
    from conftest import CASES

    for c in CASES:
        if c["schema"] == "event" and c["valid"] and c["data"]["type"] != "event.undone":
            store.accept(c["data"])
    assert store.counts()["metrics"] > 30
    store.rebuild()


def test_telemetry_ingest_is_idempotent(store):
    samples = [case("telemetry/body-mass"), case("telemetry/hrv-rmssd-with-raw"), case("telemetry/resting-hr")]
    assert store.ingest(samples) == {"inserted": 3, "updated": 0, "unchanged": 0}
    assert store.ingest(samples) == {"inserted": 0, "updated": 0, "unchanged": 3}
    revised = {**samples[0], "value": 82.1}
    assert store.ingest([revised]) == {"inserted": 0, "updated": 1, "unchanged": 0}
    assert store.counts()["telemetry_samples"] == 3
    row = store.rows("SELECT * FROM telemetry_samples WHERE source = 'hume'")[0]
    assert row["value"] == 82.1 and row["device_id"] == "hume-body-pod"


def test_telemetry_keeps_the_raw_source_record(store):
    store.ingest([case("telemetry/hrv-rmssd-with-raw")])
    sample = store.rows("SELECT * FROM telemetry_samples")[0]
    raw = store.rows("SELECT * FROM raw_records WHERE id = ?", [sample["raw_record_id"]])[0]
    assert json.loads(raw["payload"])["raw"] == {"hrv_rmssd_milli": 61.2}
    assert raw["format"] == "byobot.telemetry.v0"


def test_sleep_stage_is_a_category(store):
    store.ingest([case("telemetry/sleep-stage")])
    row = store.rows("SELECT value, category, unit FROM telemetry_samples")[0]
    assert row == {"value": None, "category": "asleepDeep", "unit": None}


def test_invalid_telemetry_batch_is_all_or_nothing(store):
    bad = {**case("telemetry/body-mass"), "unit": "lb"}
    with pytest.raises(ValidationFailed) as err:
        store.ingest([case("telemetry/resting-hr"), bad])
    assert {d["index"] for d in err.value.details} == {1}
    assert store.counts()["telemetry_samples"] == 0


def test_read_only_query(store):
    seed(store)
    result = query(store.path, "SELECT activity_id, round FROM performed_efforts")
    assert result == {"columns": ["activity_id", "round"], "rows": [{"activity_id": "barbell_back_squat", "round": 2}],
                      "truncated": False}
    assert query(store.path, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) "
                             "SELECT x FROM n", limit=3)["truncated"] is True


@pytest.mark.parametrize("sql", [
    "INSERT INTO meta VALUES ('a', 'b')",
    "UPDATE activities SET name = 'x'",
    "DELETE FROM performed_efforts",
    "DROP TABLE activities",
    "CREATE TABLE x (y)",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA journal_mode=DELETE",
    "SELECT 1; DELETE FROM performed_efforts",
])
def test_query_refuses_anything_but_reads(store, sql):
    seed(store)
    with pytest.raises(ValidationFailed):
        query(store.path, sql)
    assert store.counts()["performed_efforts"] == 1


# -- event order: event time wins, device id breaks ties (arrival order is only an audit trail) --

def _ev(event_id, ts, device, name, kind="activity.defined"):
    return {"id": event_id, "schema_version": 0, "type": kind, "ts": ts, "device_id": device,
            "actor": {"kind": "human", "id": "me"}, "payload": {"id": "plank", "name": name}}


def _name(store):
    return store.rows("SELECT name FROM activities WHERE id = 'plank'")[0]["name"]


def test_latest_event_time_wins_whatever_the_arrival_order(tmp_path):
    from byobot.store import Store

    older = _ev("0193a0b0-0000-7000-8000-0000000000a1", "2026-11-20T10:00:00Z", "phone", "Older")
    newer = _ev("0193a0b0-0000-7000-8000-0000000000a2", "2026-11-20T11:00:00Z", "laptop", "Newer")
    for i, order in enumerate(([older, newer], [newer, older])):
        with Store(tmp_path / f"{i}.db") as s:
            for e in order:
                s.accept(e)
            assert _name(s) == "Newer"
            assert s.rebuild()["ledger"]["applied"] == 2 and _name(s) == "Newer"


def test_event_times_compare_as_instants_across_offsets(store):
    # 06:30-05:00 is 11:30Z, later than 11:00Z even though it sorts earlier as text.
    store.accept(_ev("0193a0b0-0000-7000-8000-0000000000b1", "2026-11-20T06:30:00-05:00", "phone", "Phone, later"))
    store.accept(_ev("0193a0b0-0000-7000-8000-0000000000b2", "2026-11-20T11:00:00Z", "laptop", "Laptop, earlier"))
    assert _name(store) == "Phone, later"


def test_device_id_breaks_exact_ties(tmp_path):
    from byobot.store import Store

    a = _ev("0193a0b0-0000-7000-8000-0000000000c2", "2026-11-20T10:00:00.500Z", "device-a", "From a")
    b = _ev("0193a0b0-0000-7000-8000-0000000000c1", "2026-11-20T10:00:00.5Z", "device-b", "From b")
    for i, order in enumerate(([a, b], [b, a])):
        with Store(tmp_path / f"{i}.db") as s:
            for e in order:
                s.accept(e)
            assert _name(s) == "From b"


def test_order_key_handles_short_device_ids_and_fractions():
    from byobot.contract import event_order_key, instant_key
    from byobot.store import _order_key_text

    assert instant_key("2026-11-20T06:30:00.12-05:00") == "2026-11-20T11:30:00.120000000Z"
    ab = _ev("0193a0b0-0000-7000-8000-0000000000d1", "2026-11-20T10:00:00Z", "ab", "x")
    a = _ev("0193a0b0-0000-7000-8000-0000000000d2", "2026-11-20T10:00:00Z", "a", "x")
    assert (event_order_key(a) < event_order_key(ab)) == (_order_key_text(a) < _order_key_text(ab))


def test_stores_from_before_event_time_order_are_replayed_once(byobot_home):
    from byobot.store import Store

    path = byobot_home / "legacy.db"
    with Store(path) as s:
        s.accept(_ev("0193a0b0-0000-7000-8000-0000000000e2", "2026-11-20T11:00:00Z", "laptop", "Newer"))
        # Simulate the old arrival-order projection: the older event arrived last and overwrote the row.
        s.conn.execute("INSERT INTO events (id, schema_version, type, ts, device_id, actor_kind, actor_id, payload,"
                       " envelope, recorded_at) VALUES ('0193a0b0-0000-7000-8000-0000000000e1', 0, 'activity.defined',"
                       " '2026-11-20T10:00:00Z', 'phone', 'human', 'me', '{}', ?, 'x')",
                       (json.dumps(_ev("0193a0b0-0000-7000-8000-0000000000e1", "2026-11-20T10:00:00Z", "phone", "Older")),))
        s.conn.execute("UPDATE activities SET name = 'Older'")
        s.conn.execute("DELETE FROM meta WHERE key = 'ledger_head'")
    with Store(path) as s:
        assert _name(s) == "Newer"


def test_uuid7_is_monotonic_within_a_millisecond():
    from byobot.util import uuid7

    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert all(i[14] == "7" and i[19] in "89ab" for i in ids)


# -- corrections -----------------------------------------------------------

def _envelope(store, type_, payload, ts, device_id=None, actor=ME):
    from byobot.util import uuid7

    return {"id": uuid7(), "schema_version": 0, "type": type_, "ts": ts, "device_id": device_id or store.device_id,
            "actor": actor, "payload": payload}


def test_correcting_an_effort_keeps_its_time_and_round(store):
    seed(store)
    original = store.effort("eff-1")
    fixed = {**original, "metrics": {"reps": {"value": 4}, "load": {"value": 102.5, "unit": "kg"}}, "notes": "miscounted"}
    store.append("effort.performed", fixed, ME)
    now = store.effort("eff-1")
    assert (now["performed_at"], now["round"], now["planned_effort_id"]) == \
        (original["performed_at"], original["round"], original["planned_effort_id"])
    assert now["metrics"] == fixed["metrics"] and now["notes"] == "miscounted"
    assert store.rows("SELECT count(*) AS n FROM performed_efforts WHERE id = 'eff-1'")[0]["n"] == 1
    sets = store.rows("SELECT * FROM strength_sets WHERE effort_id = 'eff-1'")
    assert len(sets) == 1 and (sets[0]["reps"], sets[0]["load_kg"], sets[0]["rpe"]) == (4, 102.5, None)
    squat = store.rows("SELECT * FROM plan_vs_actual WHERE performed_effort_id = 'eff-1'")
    assert len(squat) == 1 and (squat[0]["round"], squat[0]["actual_reps"], squat[0]["actual_load_kg"]) == (2, 4, 102.5)


def test_undoing_a_correction_restores_the_effort(store):
    seed(store)
    before = store.effort("eff-1")
    fix = store.append("effort.performed", {**before, "metrics": {"reps": {"value": 3}}}, ME)
    store.append("event.undone", {"event_id": fix["id"]}, ME)
    assert store.effort("eff-1") == before


def test_a_correction_that_arrives_first_still_wins(store):
    """A phone corrected a set at 12:10 and the laptop gets that before the original from 12:00."""
    seed(store)
    effort = {"id": "late-1", "activity_id": "barbell_back_squat", "performed_at": "2026-11-21T12:00:00Z",
              "planned_effort_id": "e-squat", "round": 3, "metrics": {"reps": {"value": 5}}}
    phone = "0193a0b0-0000-7000-8000-00000000ab01"
    fix = _envelope(store, "effort.performed", {**effort, "metrics": {"reps": {"value": 4}}}, "2026-11-21T12:10:00Z", phone)
    original = _envelope(store, "effort.performed", effort, "2026-11-21T12:00:05Z", phone)
    store.accept(fix)
    store.accept(original)
    now = store.effort("late-1")
    assert now["metrics"] == {"reps": {"value": 4}} and now["performed_at"] == "2026-11-21T12:00:00Z"
    assert now["round"] == 3


def test_effort_not_found(store):
    with pytest.raises(NotFound):
        store.effort("nope")


def test_activities_keep_targets_equipment_and_ratings(store):
    store.append("activity.defined", payload("event/activity-with-targets"), ME)
    row = store.rows("SELECT * FROM activities WHERE id = 'pull_up'")[0]
    assert json.loads(row["targets"]) == {"main": ["lats"], "synergists": ["biceps", "traps", "forearms"]}
    assert json.loads(row["equipment"]) == ["pull_up_bar"]
    assert (row["movement_type"], row["fatigue"]) == ("compound", 3)
    assert json.loads(row["resistance_curve"]) == {"stretch": 3, "middle": 3, "contract": 1}


def test_plan_vs_actual_shows_a_swap(store):
    seed(store)
    swap = {**payload("event/effort-performed-lift"), "id": "eff-2", "round": 3, "activity_id": "goblet_squat",
            "notes": "Swapped for barbell back squat: rack taken"}
    store.append("effort.performed", swap, ME)
    rows = {r["performed_effort_id"]: r for r in store.rows("SELECT * FROM plan_vs_actual WHERE planned_effort_id = 'e-squat'")}
    assert (rows["eff-1"]["actual_activity_id"], rows["eff-1"]["swapped"]) == ("barbell_back_squat", 0)
    assert (rows["eff-2"]["actual_activity_id"], rows["eff-2"]["swapped"]) == ("goblet_squat", 1)


def test_older_stores_are_rebuilt_for_the_new_columns(byobot_home):
    from byobot.store import STORE_VERSION, Store

    path = byobot_home / "v1.db"
    with Store(path) as s:
        s.append("activity.defined", payload("event/activity-with-targets"), ME)
        for (view,) in s.conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
            s.conn.execute(f"DROP VIEW {view}")
        s.conn.execute("ALTER TABLE activities DROP COLUMN targets")
        s.conn.execute("PRAGMA user_version=1")
    with Store(path) as s:
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == STORE_VERSION
        assert json.loads(s.rows("SELECT targets FROM activities")[0]["targets"])["main"] == ["lats"]
        s.rows("SELECT swapped FROM plan_vs_actual")
        s.rows("SELECT * FROM daily")


# -- locations -------------------------------------------------------------

def test_locations_and_where_things_happened(store):
    store.append("location.defined", payload("event/location-defined"), ME)
    store.append("location.defined", {"id": "home", "name": "Garage"}, ME)
    assert store.rows("SELECT id, name, notes FROM locations") == [{"id": "home", "name": "Garage", "notes": None}]
    store.append("session.planned", payload("event/session-planned-at-location"), AGENT)
    store.append("session.performed", payload("event/session-performed-at-location"), ME)
    store.append("effort.performed", payload("event/effort-performed-at-location"), ME)
    assert store.rows("SELECT location_id FROM planned_sessions") == [{"location_id": "home"}]
    assert store.rows("SELECT location_id FROM performed_sessions") == [{"location_id": "example-gym"}]
    efforts = {r["id"][-3:]: r["location_id"] for r in store.rows("SELECT id, location_id FROM performed_efforts")}
    assert efforts == {"421": None, "430": "hotel-gym"}  # an effort keeps only what it was written with
    assert store.effort("0193a0b0-0000-7000-8000-000000000430")["location_id"] == "hotel-gym"


def test_views_give_each_set_its_location(store):
    store.append("session.planned", payload("event/session-planned-at-location"), AGENT)
    session = payload("event/session-performed-at-location")
    session["efforts"][0]["planned_effort_id"] = "0193a0b0-0000-7000-8000-000000000412"
    store.append("session.performed", session, ME)
    store.append("effort.performed", payload("event/effort-performed-at-location"), ME)
    sets = {r["activity_id"]: r["location_id"] for r in store.rows("SELECT * FROM strength_sets")}
    assert sets == {"barbell_back_squat": "example-gym", "dumbbell_bench_press": "hotel-gym"}
    row = store.rows("SELECT planned_location_id, location_id, actual_load_kg FROM plan_vs_actual"
                     " WHERE performed_effort_id IS NOT NULL")
    assert row == [{"planned_location_id": "home", "location_id": "example-gym", "actual_load_kg": 95}]


# -- the shipped library ---------------------------------------------------

def write_library(root, *activities):
    (root / "activities").mkdir(parents=True, exist_ok=True)
    (root / "vocabulary.json").write_text("{}")
    for a in activities:
        (root / "activities" / f"{a['id']}.json").write_text(json.dumps(a))
    return root


def test_library_activities_sit_under_your_own(byobot_home, tmp_path, monkeypatch):
    from byobot.store import Store

    shipped = {"id": "pull_up", "name": "Pull-up", "targets": {"main": ["lats"]}, "fatigue": 3}
    monkeypatch.setenv("BYOBOT_LIBRARY", str(write_library(tmp_path / "lib", shipped)))
    with Store(byobot_home / "byobot.db") as s:
        row = s.rows("SELECT name, source, fatigue, event_id FROM activities")[0]
        assert row == {"name": "Pull-up", "source": "library", "fatigue": 3, "event_id": None}
        event = s.append("activity.defined", {"id": "pull_up", "name": "Strict pull-up", "fatigue": 4}, ME)
        assert s.rows("SELECT name, source, fatigue FROM activities") == [
            {"name": "Strict pull-up", "source": "ledger", "fatigue": 4}]
        s.append("event.undone", {"event_id": event["id"]}, ME)
        assert s.rows("SELECT name, source FROM activities") == [{"name": "Pull-up", "source": "library"}]


def test_a_changed_library_is_picked_up_on_open(byobot_home, tmp_path, monkeypatch):
    from byobot.store import Store

    root = write_library(tmp_path / "lib", {"id": "pull_up", "name": "Pull-up"})
    monkeypatch.setenv("BYOBOT_LIBRARY", str(root))
    with Store(byobot_home / "byobot.db") as s:
        s.append("activity.defined", {"id": "plank", "name": "Plank"}, ME)
    root2 = write_library(tmp_path / "lib2", {"id": "pull_up", "name": "Pull-up"}, {"id": "dip", "name": "Dip"})
    monkeypatch.setenv("BYOBOT_LIBRARY", str(root2))
    with Store(byobot_home / "byobot.db") as s:
        assert [r["id"] for r in s.rows("SELECT id FROM activities ORDER BY id")] == ["dip", "plank", "pull_up"]
