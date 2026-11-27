"""The core store and its commands, on small hand-made histories."""

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from byobot.core import commands, sources
from byobot.core.contract import Contract
from byobot.core.store import Store, query
from byobot.util import Conflict, NotFound, ValidationFailed

ME = {"kind": "human", "id": "me"}
T0 = datetime(2026, 3, 2, 10, tzinfo=timezone(timedelta(hours=-5)))


class Wall:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


def effort(reps=5):
    return {"activity_id": "row", "time": {"start": "2026-03-02T10:00:00-05:00"},
            "metrics": {"reps": {"value": reps}}}


@pytest.fixture
def pair(tmp_path):
    a = Store(tmp_path / "a.db", device_id="phone", wall=Wall())
    b = Store(tmp_path / "b.db", device_id="laptop", wall=Wall())
    yield a, b
    a.close()
    b.close()


def sync(a, b):
    b.accept_many(a.events())
    a.accept_many(b.events())


def reps(store, rid):
    return store.get("effort", rid)["payload"]["metrics"]["reps"]["value"]


def test_a_new_store_records_who_it_is(pair):
    a, _ = pair
    assert a.device_id == "phone" and len(a.store_key) == 32
    assert a.conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_log_refuses_an_id_in_use(pair):
    a, _ = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    with pytest.raises(Conflict):
        commands.log(a, ME, [{"kind": "effort", "id": e["record"]["id"], "payload": effort(9)}])


def test_concurrent_edits_conflict_until_resolved(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    sync(a, b)
    commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    commands.edit(b, ME, "effort", rid, {"metrics": {"reps": {"value": 7}}})
    sync(a, b)
    [found] = a.conflicts()
    assert sorted(reps_of(v) for v in found["versions"]) == [6, 7]
    assert reps(a, rid) == reps(b, rid) and a.get("effort", rid)["conflict"]
    commands.resolve(b, ME, "effort", rid, found["versions"][0]["id"])
    sync(a, b)
    assert a.conflicts() == b.conflicts() == [] and reps(a, rid) == reps(b, rid)


def reps_of(event):
    return event["payload"]["metrics"]["reps"]["value"]


def test_identical_concurrent_edits_and_sequential_edits_do_not_conflict(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    sync(a, b)
    commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 8}}})
    commands.edit(b, ME, "effort", rid, {"metrics": {"reps": {"value": 8}}})
    sync(a, b)
    assert a.conflicts() == [] and reps(b, rid) == 8


def test_undo_writes_the_previous_version_back_and_undo_of_a_log_removes(pair):
    a, _ = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    edited = commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 9}}})
    [back] = commands.undo(a, ME, edited["id"])
    assert back["reverts"] == edited["id"] and reps(a, rid) == 5
    with pytest.raises(Conflict, match="edit it instead"):  # the log isn't current any more
        commands.undo(a, ME, e["id"])
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    commands.undo(a, ME, e["id"])
    assert a.get("effort", rid) is None
    assert a.conn.execute("SELECT count(*) FROM efforts WHERE id = ?", (rid,)).fetchone()[0] == 0


def test_undo_takes_back_a_whole_batch(pair):
    a, _ = pair
    session = {"time": {"start": "2026-03-02T10:00:00-05:00"}}
    written = commands.log(a, ME, [{"kind": "session", "payload": session}, {"kind": "effort", "payload": effort()}])
    undone = commands.undo(a, ME, written[-1]["id"])
    assert [u["op"] for u in undone] == ["retract", "retract"] and len({u["batch"] for u in undone}) == 1
    assert a.records() == []


def test_redelivery_is_a_duplicate_and_a_forged_id_is_rejected(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    assert b.accept(e) == ("accepted", "")
    assert b.accept(e) == ("duplicate", "")
    forged = {**e, "payload": effort(50)}
    assert b.accept(forged)[0] == "rejected"
    assert b.accept({"id": 1})[0] == "rejected"


def test_an_undo_arriving_before_its_target_is_kept_and_applies_later(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    edited = commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 9}}})
    [back] = commands.undo(a, ME, edited["id"])
    assert [o for o, _ in b.accept_many([back, edited, e])] == ["accepted"] * 3
    assert reps(b, rid) == 5 and b.conflicts() == []


def test_unknown_kinds_and_versions_are_kept_then_projected_after_an_upgrade(pair, tmp_path):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    newer = {**e, "id": "019aa000-0000-7000-8000-0000000000aa", "kind_version": 2,
             "payload": {**e["payload"], "bar_speed": {"value": 0.6}}}
    meal = {**e, "id": "019aa000-0000-7000-8000-0000000000bb", "record": {"kind": "meal", "id": e["record"]["id"]}}
    assert [o for o, _ in b.accept_many([newer, meal])] == ["stored_unprojected"] * 2
    assert b.get("effort", e["record"]["id"]) is None
    b.use(Contract.shipped().newer(2, {"meal": {"version": 2, "description": "A meal."}}))
    assert b.get("effort", e["record"]["id"])["payload"]["bar_speed"] == {"value": 0.6}
    assert b.get("meal", e["record"]["id"]) is not None


def test_rebuild_equals_incremental_state(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    sync(a, b)
    commands.edit(a, ME, "effort", e["record"]["id"], {"metrics": {"reps": {"value": 6}}})
    commands.edit(b, ME, "effort", e["record"]["id"], {"metrics": {"reps": {"value": 7}}})
    sync(a, b)
    tables = ("records", "efforts", "metrics")
    before = {t: a.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables}
    a.rebuild()
    assert {t: a.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables} == before


def test_the_log_is_append_only(pair):
    a, _ = pair
    commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    with pytest.raises(sqlite3.DatabaseError):
        a.conn.execute("DELETE FROM events")


def test_a_device_a_day_ahead_is_flagged(pair):
    a, b = pair
    a.wall = Wall(T0 + timedelta(days=3))
    commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    sync(a, b)
    assert [w["device_id"] for w in b.clock_warnings()] == ["phone"]


def test_an_event_from_far_in_the_future_is_refused(pair):
    """One event stamped near the end of time used to stop every later write here."""
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    forged = {**e, "id": a.new_id(), "record": {**e["record"], "id": a.new_record_id("effort")},
              "hlc": "9999999999999-000000"}
    assert b.accept_many([forged])[0][0] == "rejected"
    commands.log(b, ME, [{"kind": "effort", "payload": effort()}])


def test_a_store_whose_clock_ran_away_writes_again(tmp_path):
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        commands.log(s, ME, [{"kind": "effort", "payload": effort()}])
        s._set("hlc", "9999999999999-999999")
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        commands.log(s, ME, [{"kind": "effort", "payload": effort()}])


def test_edit_and_remove_need_a_live_record(pair):
    a, _ = pair
    with pytest.raises(NotFound):
        commands.edit(a, ME, "effort", "019aa000-0000-7000-8000-000000000001", {"round": 2})
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    commands.remove(a, ME, "effort", e["record"]["id"])
    with pytest.raises(NotFound):
        commands.remove(a, ME, "effort", e["record"]["id"])


def test_reopening_with_an_older_contract_stops_projecting_what_it_can_not_read(pair, tmp_path):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    newer = {**e, "id": "019aa000-0000-7000-8000-0000000000aa", "kind_version": 2}
    b.use(Contract.shipped().newer(2))
    assert b.accept(newer) == ("accepted", "")
    b.close()
    reopened = Store(tmp_path / "b.db")
    assert reopened.get("effort", e["record"]["id"]) is None
    reopened.close()


def test_a_newer_payload_this_install_can_not_check_is_still_kept(pair):
    _, b = pair
    odd = {"id": "019aa000-0000-7000-8000-0000000000cc", "schema_version": 1,
           "record": {"kind": "plan", "id": "019aa000-0000-7000-8000-000000000101"}, "op": "put", "kind_version": 3,
           "hlc": "1795100000000-000000", "ts": "2026-11-20T18:00:00-05:00", "device_id": "example-laptop",
           "actor": {"kind": "human", "id": "me"}, "prev": [], "payload": {"time": "soon"}}
    assert b.accept(odd) == ("stored_unprojected", "")
    assert b.accept({**odd, "id": "019aa000-0000-7000-8000-0000000000cd", "payload": {"x": float("nan")}})[0] \
        == "rejected"


def later(e, **extra):
    return {**e, "id": "019aa000-0000-7000-8000-0000000000dd", "schema_version": 2, "kind_version": 3,
            "prev": [e["id"]], "context": {"note": "a field schema 2 added"}, **extra}


def test_a_later_schema_version_is_kept_and_relayed_as_received_never_applied(pair, tmp_path):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid, ahead = e["record"]["id"], later(e, payload={"sets": 5})
    assert b.accept_many([e, ahead]) == [("accepted", ""), ("stored_unprojected", "")]
    assert b.event(ahead["id"]) == ahead and b.events()[-1] == ahead
    assert reps(b, rid) == 5 and b.accept(ahead) == ("duplicate", "")
    assert b.accept({**ahead, "context": {}})[0] == "rejected"
    with pytest.raises(Conflict, match="newer byobot"):
        commands.edit(b, ME, "effort", rid, {"metrics": {"reps": {"value": 9}}})
    b.rebuild()
    b.use(Contract.shipped().newer(3))
    assert reps(b, rid) == 5
    with Store(tmp_path / "c.db") as c:
        c.restore(b.export())
        assert c.event(ahead["id"]) == ahead
        assert c.accept({**ahead, "id": "019aa000-0000-7000-8000-0000000000de", "hlc": "bad"})[0] == "rejected"


def test_an_erase_from_a_later_schema_version_is_honored(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    gone = later(e, op="erase", payload={})
    del gone["kind_version"]
    assert b.accept_many([e, gone])[1] == ("stored_unprojected", "")
    assert b.get("effort", e["record"]["id"]) is None and b.event(e["id"])["payload"] == {}


def test_an_estimate_keeps_its_bounds_and_attachments(pair):
    a, _ = pair
    photo = [{"id": "019aa000-0000-7000-8000-00000000020a", "media_type": "image/jpeg", "bytes": 482113,
              "sha256": "0c9a0bd1b7a2d5a9e6a8d1b4f3e2c1a09f8e7d6c5b4a39281706f5e4d3c2b1a0"}]
    [o] = commands.log(a, ME, [{"kind": "observation", "payload": {
        "code": "body_mass", "value": 180, "min": 176, "max": 184, "unit": "[lb_av]", "modality": "estimated",
        "time": {"start": "2026-03-02T10:00:00-05:00"}, "attachments": photo}}])
    row = a.rows("SELECT value, min, max, round(std_min, 1) AS low, round(std_max, 1) AS high FROM observations")
    assert row == [{"value": 180, "min": 176, "max": 184, "low": 79.8, "high": 83.5}]
    assert a.get("observation", o["record"]["id"])["payload"]["attachments"] == photo


def test_an_observation_may_be_a_range_alone(pair):
    a, _ = pair
    commands.log(a, ME, [{"kind": "observation", "payload": {
        "code": "heart_rate", "min": 48, "max": 71, "unit": "/min", "modality": "sensed",
        "time": {"start": "2026-03-02T00:30:00-05:00", "end": "2026-03-02T06:40:00-05:00"}}}])
    row = a.rows("SELECT value, min, max, std_min, std_max FROM observations")
    assert row == [{"value": None, "min": 48, "max": 71, "std_min": 48, "std_max": 71}]


def test_local_dates_and_offsets_come_from_the_record(pair):
    a, _ = pair
    [late] = commands.log(a, ME, [{"kind": "effort", "payload": {
        **effort(), "time": {"start": "2026-11-21T00:30:00+05:00"}}}])
    [walk] = commands.log(a, ME, [{"kind": "session", "payload": {
        "time": {"start": "2026-03-08T01:30:00-05:00", "end": "2026-03-08T03:30:00-04:00"}}}])
    row = a.conn.execute("SELECT local_date, offset_min FROM efforts WHERE id = ?", (late["record"]["id"],)).fetchone()
    assert tuple(row) == ("2026-11-21", 300)
    start, end = a.conn.execute("SELECT start_ms, end_ms FROM sessions WHERE id = ?",
                                (walk["record"]["id"],)).fetchone()
    assert end - start == 3600 * 1000


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_log_writes_the_offset_in_force(pair, monkeypatch):
    a, _ = pair
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        [now, summer] = commands.log(a, ME, [
            {"kind": "observation", "payload": {"code": "body_mass", "value": 80, "unit": "kg",
                                                "modality": "self_reported"}},
            {"kind": "effort", "payload": {**effort(), "time": {"start": "2026-07-01T07:00:00"}}}])
    finally:
        monkeypatch.undo()
        time.tzset()
    assert now["payload"]["time"]["start"].endswith("-05:00")
    assert summer["payload"]["time"]["start"] == "2026-07-01T07:00:00.000-04:00"


def test_a_home_zone_dates_times_written_without_an_offset(pair):
    a, _ = pair
    commands.log(a, ME, [{"kind": "setting", "id": "home_zone", "payload": {"value": "Asia/Tokyo"}}])
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": {**effort(), "time": {"start": "2026-03-02T07:00:00"}}}])
    assert e["payload"]["time"]["start"] == "2026-03-02T07:00:00.000+09:00"


def test_commands_hold_writes_to_the_registry(pair):
    a, _ = pair
    bad = [({"reps": {"value": 5}, "x.grip": {"value": "wide"}}, None),
           ({"tempo_typo": {"value": 1}}, "unknown code tempo_typo"),
           ({"load": {"value": 100, "unit": "stone"}}, "load has no unit stone"),
           ({"body_mass": {"value": 80, "unit": "kg"}}, "body_mass isn't used on effort")]
    for metrics, refusal in bad:
        record = {"kind": "effort", "payload": {**effort(), "metrics": metrics}}
        if refusal is None:
            commands.log(a, ME, [record])
        else:
            with pytest.raises(ValidationFailed, match=refusal):
                commands.log(a, ME, [record])
    with pytest.raises(ValidationFailed, match="sleep_stage has no value napping"):
        commands.observe(a, ME, "sleep_stage", "napping", start="2026-03-02T01:00:00-05:00")
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    with pytest.raises(ValidationFailed, match="unknown code tempo_typo"):
        commands.edit(a, ME, "effort", e["record"]["id"], {"metrics": {"tempo_typo": {"value": 1}}})


def test_measures_are_kept_in_each_codes_own_unit_too(pair):
    a, _ = pair
    lb = commands.log(a, ME, [{"kind": "effort", "payload": {**effort(), "metrics": {
        "load": {"value": 100, "unit": "[lb_av]"}, "rest": {"min": 1, "max": 2, "unit": "min"}}}}])[0]
    rows = {r[0]: tuple(r)[1:] for r in a.conn.execute(
        "SELECT code, std_value, std_min, std_max, std_unit FROM metrics WHERE owner_id = ?", (lb["record"]["id"],))}
    assert rows["load"] == (pytest.approx(45.359237), None, None, "kg")
    assert rows["rest"] == (None, 60, 120, "s")
    weight = commands.observe(a, ME, "body_mass", 180, "[lb_av]", start="2026-03-02T07:00:00-05:00")
    row = a.conn.execute("SELECT std_value, std_unit, modality FROM observations WHERE id = ?", (weight["record"]["id"],))
    assert tuple(row.fetchone()) == (pytest.approx(81.6466266), "kg", "self_reported")


def test_derived_tables_rebuild_when_their_shape_changes(tmp_path):
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        commands.observe(s, ME, "body_mass", 80, "kg", start="2026-03-02T07:00:00-05:00")
        s.conn.execute("ALTER TABLE observations DROP COLUMN std_value")
        s._set("shape", "older")
        s.conn.commit()
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        assert tuple(s.conn.execute("SELECT std_value FROM observations").fetchone()) == (80,)


def test_library_activities_ship_and_yours_replace_them(tmp_path, monkeypatch):
    monkeypatch.delenv("BYOBOT_LIBRARY")
    a = Store(tmp_path / "lib.db", device_id="phone", wall=Wall())
    name = lambda: a.conn.execute("SELECT name, source FROM activities WHERE id = 'ab_wheel'").fetchone()  # noqa: E731
    assert tuple(name()) == ("Ab Wheel", "library")
    [e] = commands.log(a, ME, [{"kind": "activity", "id": "ab_wheel", "payload": {"name": "Wheel rollout"}}])
    assert tuple(name()) == ("Wheel rollout", "store")
    commands.remove(a, ME, "activity", "ab_wheel")
    assert tuple(name()) == ("Ab Wheel", "library")
    a.rebuild()
    assert tuple(name()) == ("Ab Wheel", "library")
    a.close()


def test_paired_devices_stay_out_of_the_log(pair):
    a, _ = pair
    a.pair("iphone-1", "Example iPhone", "ab" * 32)
    assert a.peer_for_token("ab" * 32)["device_id"] == "iphone-1" and a.peer_for_token("cd" * 32) is None
    a.unpair("iphone-1")
    assert a.peer_for_token("ab" * 32) is None and a.peers()[0]["revoked_at"]
    assert a.events() == []
    with pytest.raises(NotFound):
        a.unpair("nobody")


def test_queries_read_and_refuse_writes_the_key_and_token_hashes(tmp_path):
    from byobot.core.store import query

    with Store(tmp_path / "s.db", wall=Wall()) as s:
        commands.log(s, ME, [{"kind": "effort", "payload": effort()}])
        s.pair("example-phone", "Example iPhone", "ab" * 32)
    found = query(tmp_path / "s.db", "SELECT count(*) AS n FROM efforts")
    assert found["rows"] == [{"n": 1}]
    assert query(tmp_path / "s.db", "SELECT device_id FROM peers")["rows"] == [{"device_id": "example-phone"}]
    for sql in ("SELECT * FROM meta", "SELECT token_sha256 FROM peers", "DELETE FROM efforts",
                "PRAGMA user_version", "ATTACH 'x.db' AS x"):
        with pytest.raises(ValidationFailed):
            query(tmp_path / "s.db", sql)


def test_queries_read_only_documented_tables_and_columns(tmp_path):
    from byobot.cloud.keeper import DDL as CLOUD_DDL

    with Store(tmp_path / "s.db", wall=Wall()) as s:
        s.conn.executescript(CLOUD_DDL + "CREATE TABLE stray (secret TEXT);")
        cloud = [r[0] for r in s.conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'cloud_%'")]
    assert cloud
    for table in [*cloud, "stray"]:
        with pytest.raises(ValidationFailed):
            query(tmp_path / "s.db", f"SELECT * FROM {table}")
        with pytest.raises(ValidationFailed):
            query(tmp_path / "s.db", f"WITH c AS (SELECT * FROM {table}) SELECT * FROM c")
    assert query(tmp_path / "s.db", "SELECT count(*) AS n FROM efforts")["rows"] == [{"n": 0}]


def test_an_older_store_is_refused_with_what_to_do(tmp_path):
    from byobot.util import ByobotError

    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("PRAGMA user_version = 4")
    old.close()
    with pytest.raises(ByobotError, match="move it aside"):
        Store(tmp_path / "old.db")


def test_logged_parts_get_ids_and_observations_a_modality(tmp_path):
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        [e] = commands.log(s, ME, [{"kind": "planned_session", "payload": {
            "scheduled_on": "2026-03-03", "name": "Legs", "blocks": [{"efforts": [{"activity_id": "row"}]}]}}])
        block = e["payload"]["blocks"][0]
        assert block["id"] == f"{e['record']['id']}.0" and block["efforts"][0]["id"] == f"{e['record']['id']}.0.0"
        [e] = commands.log(s, ME, [{"kind": "effort", "payload": {"activity_id": "row"}}])
        assert "modality" not in e["payload"] and "start" in e["payload"]["time"]
        [o] = commands.log(s, ME, [{"kind": "observation", "payload": {"code": "body_mass", "value": 80, "unit": "kg"}}])
        assert o["payload"]["modality"] == "self_reported"


def test_samples_deleted_by_id_alone_are_found_by_their_source(tmp_path):
    from byobot.core import sources

    sample = {"source": "apple-health", "source_id": "h1", "type": "HKQuantityTypeIdentifierBodyMass",
              "start": "2026-03-02T07:00:00Z", "end": "2026-03-02T07:00:00Z", "value": 80, "unit": "kg"}
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        assert {k: len(v) for k, v in sources.ingest_samples(s, [sample]).items()} == {"apple_health": 1}
        assert s.counts()["observations"] == 1
        written = sources.ingest_samples(s, [], [{"source": "apple_health", "source_id": "h1"}])
        assert [e["op"] for e in written["apple_health"]] == ["retract"]
        assert s.counts()["observations"] == 0
        with pytest.raises(ValidationFailed):
            sources.ingest_samples(s, [{"source": "apple_health"}])


def test_erase_cleanup_waits_out_a_reader(tmp_path):
    """While another connection reads, erased content can stay on disk; the next write clears it."""
    def held():
        return b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("s.db"))

    with Store(tmp_path / "s.db", wall=Wall()) as s:
        commands.log(s, ME, [{"kind": "location", "id": "gym", "payload": {"name": "erase-me-7f3a"}}])
        reader = sqlite3.connect(tmp_path / "s.db")
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM events").fetchone()
        started = time.monotonic()
        commands.erase(s, ME, "location", "gym")
        assert time.monotonic() - started < 2 and s.compact_pending()
        reader.close()
        commands.log(s, ME, [{"kind": "location", "id": "pool", "payload": {"name": "pool"}}])
        assert not s.compact_pending() and b"erase-me-7f3a" not in held()


def test_query_stops_at_its_deadline(tmp_path):
    with Store(tmp_path / "s.db"):
        pass
    endless = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    started = time.monotonic()
    with pytest.raises(ValidationFailed, match="stopped after"):
        query(tmp_path / "s.db", endless, seconds=0.5)
    assert time.monotonic() - started < 5


def test_undo_of_an_older_version_keeps_the_later_edit(pair):
    a, _ = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    first = commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 7}}})
    commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 9}}})
    with pytest.raises(Conflict):
        commands.undo(a, ME, first["id"])
    assert reps(a, rid) == 9


def test_a_redaction_without_its_erase_blanks_nothing(pair):
    """A synced event marked erased, whose erase never came, can't blank a live record."""
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    sync(a, b)
    rid = e["record"]["id"]
    forged = {**e, "id": a.new_id(), "prev": [e["id"]], "hlc": "2026000000000-000000", "payload": {},
              "redacted_by": a.new_id()}
    forged["hlc"] = e["hlc"][:-1] + "9"
    b.accept_many([forged])
    assert reps(b, rid) == 5 and not b.record("effort", rid)["conflict"]



def test_an_export_is_one_snapshot(tmp_path):
    with Store(tmp_path / "a.db", wall=Wall()) as a, Store(tmp_path / "a.db", wall=Wall()) as writer:
        commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
        lines = a.export()
        header = next(lines)
        commands.log(writer, ME, [{"kind": "effort", "payload": effort()}])
        exported = [header, *lines]
    with Store(tmp_path / "b.db", wall=Wall()) as b:
        assert b.restore(exported)["events"] == {"accepted": len(exported) - 1}


def test_restore_refuses_an_export_with_a_bad_event(tmp_path):
    with Store(tmp_path / "a.db", wall=Wall()) as a:
        commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
        lines = list(a.export())
    lines[1] = lines[1].replace('"op":"put"', '"op":"nope"')
    with Store(tmp_path / "b.db", wall=Wall()) as b:
        with pytest.raises(ValidationFailed, match="refused"):
            b.restore(lines)
        assert b.position() == 0


def test_every_commit_is_flushed(pair):
    a, _ = pair
    assert a.conn.execute("PRAGMA synchronous").fetchone()[0] == 2



def test_erasing_a_session_can_take_its_sets(pair):
    a, _ = pair
    [s] = commands.log(a, ME, [{"kind": "session", "payload": {"name": "Legs", "time": {"start": "2026-03-02T10:00:00-05:00"}}}])
    sid = s["record"]["id"]
    linked = {**effort(), "links": {"part_of": {"kind": "session", "id": sid}}}
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": linked}])
    commands.erase(a, ME, "session", sid)
    assert commands.linked(a, "session", sid) == [("effort", e["record"]["id"])]
    [s] = commands.log(a, ME, [{"kind": "session", "payload": {"name": "Arms", "time": {"start": "2026-03-02T11:00:00-05:00"}}}])
    sid = s["record"]["id"]
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": {**effort(), "links": {"part_of": {"kind": "session", "id": sid}}}}])
    commands.erase(a, ME, "session", sid, with_efforts=True)
    assert a.record("effort", e["record"]["id"])["erased"] and commands.linked(a, "session", sid) == []


SLEEP = {"id": "s1", "start": "2026-03-02T04:00:00.000Z", "end": "2026-03-02T12:30:00.000Z",
         "timezone_offset": "-05:00", "updated_at": "2026-03-02T12:00:00.000Z", "score_state": "SCORED",
         "score": {"respiratory_rate": 15.0}}


def test_a_source_event_redelivered_after_erase_is_a_duplicate(pair):
    a, b = pair
    b.adopt(a.store_id, a._get("store_key"))
    [src] = sources.ingest(a, "whoop", [("sleep", "s1", SLEEP)])
    [mine] = sources.ingest(b, "whoop", [("sleep", "s1", SLEEP)])
    assert mine["id"] == src["id"]
    commands.erase(a, ME, "source_record", src["record"]["id"])
    assert a.accept_many(b.events())[0][0] == "duplicate"


def test_a_revision_another_import_just_wrote_writes_nothing(pair):
    a, _ = pair
    [e] = sources.ingest(a, "whoop", [("sleep", "s1", SLEEP)])
    change = {"kind": "source_record", "id": e["record"]["id"], "op": "put", "payload": e["payload"], "prev": [],
              "event_id": e["id"]}
    assert a.write({"kind": "connector", "id": "whoop"}, [change]) == []


def test_an_erased_source_record_is_not_imported_again(pair):
    a, _ = pair
    [e] = sources.ingest(a, "whoop", [("sleep", "s1", SLEEP)])
    commands.erase(a, ME, "source_record", e["record"]["id"])
    assert sources.ingest(a, "whoop", [("sleep", "s1", {**SLEEP, "updated_at": "2026-03-03T00:00:00.000Z"})]) == []


def test_accept_never_raises_on_a_huge_number(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    big = {**e, "id": "0190f000-0000-7000-8000-00000000eeee", "prev": [e["id"]],
           "payload": {**effort(), "metrics": {"reps": {"value": 5}, "x.count": {"value": 10**20}}}}
    assert b.accept(big) == ("rejected", "a number too large to store")
    assert sources.ingest(a, "whoop", [("sleep", "s1", {**SLEEP, "score": {"respiratory_rate": 10**20}})])
    assert a.rows("SELECT * FROM observations WHERE code = 'respiratory_rate'") == []
