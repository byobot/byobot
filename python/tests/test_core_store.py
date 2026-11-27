"""The core store and its commands, on small hand-made histories."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from byobot.core import commands, reducers, relay, sources
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


def observation(code, value, start, unit=None):
    payload = {"code": code, "value": value, "unit": unit, "time": {"start": start}, "modality": "self_reported"}
    return {"kind": "observation", "payload": {k: v for k, v in payload.items() if v is not None}}


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


def test_each_event_is_kept_once_and_its_columns_read_from_it(pair):
    a, b = pair
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    ahead = later(e, payload={"sets": 5}, actor="a newer shape")
    b.accept_many([e, ahead])
    tables = {r[0] for r in b.conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    assert "later_events" not in tables
    rows = b.rows("SELECT id, kind, record_id, op, kind_version, actor_kind, prev, payload, header FROM events "
                  "ORDER BY seq")
    assert [r["id"] for r in rows] == [e["id"], ahead["id"]]
    assert rows[0]["actor_kind"] == "human" and rows[1]["actor_kind"] is None and rows[1]["kind_version"] == 3
    for row, event in zip(rows, (e, ahead)):
        assert row["prev"] == json.dumps(event["prev"]) and json.loads(row["payload"]) == event["payload"]
        assert json.loads(row["header"]) == {k: v for k, v in event.items() if k != "payload"}


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
        commands.log(a, ME, [observation("sleep_stage", "napping", "2026-03-02T01:00:00-05:00")])
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
    weight = commands.log(a, ME, [observation("body_mass", 180, "2026-03-02T07:00:00-05:00", "[lb_av]")])[0]
    row = a.conn.execute("SELECT std_value, std_unit, modality FROM observations WHERE id = ?", (weight["record"]["id"],))
    assert tuple(row.fetchone()) == (pytest.approx(81.6466266), "kg", "self_reported")


def test_derived_tables_rebuild_when_their_shape_changes(tmp_path):
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        commands.log(s, ME, [observation("body_mass", 80, "2026-03-02T07:00:00-05:00", "kg")])
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
    assert query(tmp_path / "s.db", "SELECT actor_kind FROM events")["rows"] == [{"actor_kind": "human"}]
    assert query(tmp_path / "s.db", "SELECT id FROM devices")["rows"] == [{"id": "example-phone"}]
    for sql in ("SELECT * FROM meta", "SELECT token_sha256 FROM devices", "DELETE FROM efforts",
                "PRAGMA user_version", "ATTACH 'x.db' AS x"):
        with pytest.raises(ValidationFailed):
            query(tmp_path / "s.db", sql)


def test_queries_read_only_documented_tables_and_columns(tmp_path):
    with Store(tmp_path / "s.db", wall=Wall()) as s:
        s.conn.executescript("CREATE TABLE stray (secret TEXT);")
        cloud = [r[0] for r in s.conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'cloud_%'")]
    assert len(cloud) == 5
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

    sample = {"source": "apple_health", "source_id": "h1", "type": "HKQuantityTypeIdentifierBodyMass",
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


AGENT = {"kind": "agent", "id": "example-agent"}


def session(start="2026-03-02T09:00:00-05:00", end="2026-03-02T10:00:00-05:00", name="lift"):
    return {"name": name, "time": {"start": start, "end": end}}


def session_effort(sid, reps=5):
    return {"activity_id": "row", "time": {"start": "2026-03-02T09:10:00-05:00"},
            "links": {"part_of": {"kind": "session", "id": sid}}, "metrics": {"reps": {"value": reps}}}


@pytest.fixture
def one(tmp_path):
    s = Store(tmp_path / "a.db", device_id="laptop", wall=Wall())
    yield s
    s.close()


def test_a_rebuild_cut_short_after_an_upgrade_is_finished_on_the_next_open(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    with Store(path, wall=Wall()) as s:
        [a] = commands.log(s, ME, [{"kind": "session", "payload": session()}])
        s._set("shape", "an-older-shape")  # as a store written by an older build looks

    def killed(self):
        raise KeyboardInterrupt  # the process stopped while rebuilding

    monkeypatch.setattr(Store, "rebuild", killed)
    with pytest.raises(KeyboardInterrupt):
        Store(path, wall=Wall())
    monkeypatch.undo()
    with Store(path, wall=Wall()) as s:
        assert s.rows("SELECT id FROM sessions") == [{"id": a["record"]["id"]}], "derived tables left empty"


def test_erasing_a_session_with_efforts_keeps_an_effort_moved_to_another_session(one):
    [a] = commands.log(one, ME, [{"kind": "session", "payload": session(name="a")}])
    [b] = commands.log(one, ME, [{"kind": "session", "payload": session(name="b")}])
    sa, sb = a["record"]["id"], b["record"]["id"]
    [e] = commands.log(one, ME, [{"kind": "effort", "payload": session_effort(sa)}])
    rid = e["record"]["id"]
    commands.edit(one, ME, "effort", rid, {"links": {"part_of": {"kind": "session", "id": sb}}})
    commands.erase(one, ME, "session", sa, with_efforts=True)
    assert one.get("effort", rid) is not None, "an effort now in session b was erased with session a"


def test_undoing_a_resolve_brings_the_conflict_back(tmp_path):
    a = Store(tmp_path / "a.db", device_id="phone", wall=Wall())
    b = Store(tmp_path / "b.db", device_id="laptop", wall=Wall())
    b.adopt(a.store_id, a._get("store_key"))
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    b.accept_many(a.events())
    six = commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    commands.edit(b, AGENT, "effort", rid, {"metrics": {"reps": {"value": 7}}})
    a.accept_many(b.events())
    fixed = commands.resolve(a, ME, "effort", rid, six["id"])
    commands.undo(a, ME, fixed["id"])
    assert a.conflicts("effort", rid), "undo of a resolve kept only one side; the other version is no longer shown"


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def opened(path):
    old = os.umask(0o022)
    try:
        s = Store(path)
        s.conn.execute("PRAGMA user_version")
        s.close()
    finally:
        os.umask(old)


def test_a_new_store_is_readable_only_by_its_owner(tmp_path):
    home = tmp_path / "home"
    opened(home / "byobot.db")
    assert mode(home) == 0o700
    assert {f.name: mode(f) & 0o077 for f in home.iterdir() if f.name.startswith("byobot.db")} == {
        f.name: 0 for f in home.iterdir() if f.name.startswith("byobot.db")}


def test_an_existing_stores_files_are_made_private_when_opened(tmp_path):
    home = tmp_path / "home"
    opened(home / "byobot.db")
    home.chmod(0o755)
    (home / "byobot.db").chmod(0o644)
    opened(home / "byobot.db")
    assert {f.name: mode(f) for f in home.iterdir() if f.name.startswith("byobot.db")} == {
        f.name: 0o600 for f in home.iterdir() if f.name.startswith("byobot.db")}


EST = timezone(timedelta(hours=-5))


DERIVED = ("activities", "locations", "settings", "plans", "planned_sessions", "planned_blocks", "planned_efforts",
           "sessions", "efforts", "observations", "metrics", "source_records", "source_links", "source_sessions",
           "overlays", "occurrences", "occurrence_items", "occurrence_metrics")


def open_store(path, device_id="laptop", wall=None, **kw) -> Store:
    return Store(path, device_id=device_id, wall=wall or Wall(), **kw)


def whoop_workout(wid: str, start: str, end: str, updated="2026-03-02T16:00:00.000Z", strain=10.0, hr=140,
                  offset="-05:00") -> dict:
    return {"id": wid, "start": start, "end": end, "timezone_offset": offset, "updated_at": updated,
            "sport_name": "running", "score_state": "SCORED",
            "score": {"strain": strain, "average_heart_rate": hr, "max_heart_rate": hr + 30}}


def derived(store: Store) -> dict:
    """Every derived table's rows, sorted, so two stores (or one before and after a rebuild) compare exactly."""
    out = {}
    for table in DERIVED:
        rows = [json.dumps(dict(r), sort_keys=True) for r in store.conn.execute(f"SELECT * FROM {table}")]
        out[table] = sorted(rows)
    out["records"] = sorted(json.dumps(dict(r), sort_keys=True) for r in store.conn.execute("SELECT * FROM records"))
    return out


def _bad(kind, store, logged, imported):
    if kind == "lone_surrogate":  # valid JSON ("\ud800"), but not encodable as UTF-8 for SQLite
        e = copy.deepcopy(logged)
        e["id"] = "019c0000-0000-7000-8000-0000000000a1"
        e["payload"]["notes"] = json.loads('"set \\ud800"')
        return e
    if kind == "bare_id_of_a_held_source_event":  # the duplicate check reads `record` before the schema check
        return {"id": imported["id"]}
    if kind == "deep_nesting":  # canonical JSON recurses in Python
        e = copy.deepcopy(logged)
        e["id"] = "019c0000-0000-7000-8000-0000000000a2"
        deep: list = []
        for _ in range(1500):
            deep = [deep]
        e["payload"]["metrics"]["x.deep"] = {"value": 1, "unit": deep}
        return e
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["lone_surrogate", "bare_id_of_a_held_source_event", "deep_nesting"])
def test_a_bad_event_is_rejected_and_the_rest_of_its_batch_kept(tmp_path, kind):
    with open_store(tmp_path / "a.db") as s:
        [logged] = commands.log(s, ME, [{"kind": "effort", "payload": effort()}])
        [imported] = sources.ingest(s, "whoop", [("workout", "3001", whoop_workout(
            "3001", "2026-03-02T14:00:00.000Z", "2026-03-02T15:00:00.000Z"))])
        good = copy.deepcopy(logged)
        good.update(id="019c0000-0000-7000-8000-0000000000a3", prev=[logged["id"]], hlc="1900000000000-000000")
        good["payload"]["metrics"]["reps"]["value"] = 9
        out = s.accept_many([_bad(kind, s, logged, imported), good])
        assert out[0][0] == "rejected" and out[1] == ("accepted", "")


def test_an_agent_writing_a_lone_surrogate_gets_a_validation_error(tmp_path):
    with open_store(tmp_path / "a.db") as s:
        p = effort()
        p["notes"] = json.loads('"set \\udc00"')
        with pytest.raises(ValidationFailed):
            commands.log(s, ME, [{"kind": "effort", "payload": p}])


def test_editing_a_set_logged_with_a_code_this_registry_lacks_changes_what_was_asked(tmp_path):
    """A newer app logs a set with a registry code this install doesn't have yet (delivery keeps it). An edit of
    only `reps` here re-checks the whole payload against this registry and is refused, telling the person to use a
    registry code they never wrote; no edit or agent fix of that set works until this install upgrades."""
    with open_store(tmp_path / "a.db") as s:
        [first] = commands.log(s, ME, [{"kind": "effort", "payload": effort()}])
        newer = copy.deepcopy(first)
        newer.update(id="019c0000-0000-7000-8000-0000000000c1", prev=[first["id"]], hlc="1900000000000-000000",
                     device_id="phone")
        newer["payload"]["metrics"]["cadence"] = {"value": 30}  # a code from a later registry
        assert s.accept(newer) == ("accepted", "")
        rid = first["record"]["id"]
        commands.edit(s, ME, "effort", rid, {"metrics": {"reps": {"value": 7}}})
        payload = s.get("effort", rid)["payload"]
    assert payload["metrics"] == {"reps": {"value": 7}, "cadence": {"value": 30}}


def _race(path: Path, monkeypatch, first_writes: bool) -> dict:
    """Two processes open a store that doesn't exist yet. Both read user_version 0 before either commits; the
    first finishes creating it (and, if asked, imports a WHOOP workout) while the second waits for its write lock."""
    first_in_tx, release, second_read, first_done = (threading.Event() for _ in range(4))
    seed, real_tx = reducers.seed_library, Store.transaction

    def slow_seed(conn):
        if threading.current_thread().name == "first":
            first_in_tx.set()
            assert release.wait(10)
        seed(conn)

    def tx(self):
        if threading.current_thread().name == "second" and not second_read.is_set():
            second_read.set()  # user_version (0) is read; its transaction starts once the first is done
            assert first_done.wait(10)
        return real_tx(self)

    monkeypatch.setattr(reducers, "seed_library", slow_seed)
    monkeypatch.setattr(Store, "transaction", tx)
    seen = {}

    def first():
        with Store(path, wall=Wall()) as s:
            seen["first"] = (s.store_id, s._get("store_key"))
            if first_writes:
                sources.ingest(s, "whoop", [("workout", "1001", whoop_workout(
                    "1001", "2026-03-02T14:00:00.000Z", "2026-03-02T15:00:00.000Z"))])
        first_done.set()

    def second():
        with Store(path, wall=Wall()) as s:
            seen["second"] = (s.store_id, s._get("store_key"))

    a = threading.Thread(target=first, name="first")
    a.start()
    assert first_in_tx.wait(10)
    b = threading.Thread(target=second, name="second")
    b.start()
    assert second_read.wait(10)
    release.set()
    a.join(20)
    b.join(20)
    monkeypatch.undo()
    return seen


def test_two_processes_creating_one_store_keep_the_first_ones_identity(tmp_path, monkeypatch):
    seen = _race(tmp_path / "byobot.db", monkeypatch, first_writes=False)
    assert seen["first"] == seen["second"], "the second open replaced the store id and key the first handed out"


def test_a_store_created_by_two_processes_at_once_can_still_be_restored(tmp_path, monkeypatch):
    """The import made under the first key no longer verifies under the second, so the backup is refused whole."""
    path = tmp_path / "byobot.db"
    _race(path, monkeypatch, first_writes=True)
    with open_store(path) as s:
        backup = list(s.export())
    with open_store(tmp_path / "restored.db", device_id="new-laptop") as fresh:
        fresh.restore(backup)
        assert len(fresh.events()) == 1


BASE = datetime(2020, 1, 1, 9, tzinfo=EST)


def _steps_to_log_today(tmp_path, earlier: int) -> int:
    """SQLite virtual-machine steps (in hundreds) spent writing one session after `earlier` past sessions:
    deterministic, unlike wall time."""
    with open_store(tmp_path / f"n{earlier}.db") as s:
        for k in range(0, earlier, 500):
            commands.log(s, ME, [{"kind": "session", "payload": session(
                (BASE + timedelta(hours=12 * i)).isoformat(), (BASE + timedelta(hours=12 * i, minutes=60)).isoformat())}
                for i in range(k, min(earlier, k + 500))])
        count = [0]

        def tally():
            count[0] += 1
            return 0

        s.conn.set_progress_handler(tally, 100)
        at = BASE + timedelta(hours=12 * earlier)
        commands.log(s, ME, [{"kind": "session", "payload": session(at.isoformat(),
                                                                    (at + timedelta(minutes=60)).isoformat())}])
        s.conn.set_progress_handler(None, 0)
        return count[0]


def test_logging_todays_session_costs_the_same_after_years_of_workouts(tmp_path):
    """occurrences._items_in looks for workouts with `start_ms <= hi AND end >= lo`; the start_ms index bounds only
    the top, so every workout that started before today is read on every session write, WHOOP import and phone
    sync, and a rebuild or restore is quadratic. S15's growth checks use sets only, so they never see it."""
    small, large = _steps_to_log_today(tmp_path, 250), _steps_to_log_today(tmp_path, 4000)
    assert large <= 2 * small + 5, f"one session write took {small} then {large} hundred VM steps"


@pytest.mark.parametrize("kind", ["session", "observation"])
def test_an_impossible_date_is_refused_not_kept_out_of_every_read(tmp_path, kind):
    """The timestamp pattern lets 2026-02-30 through. With an end, the time-order rule parses it and the write is
    refused; with a start alone nothing parses it until the reducer, whose ValueError makes the record project as
    nothing. The write answers 'accepted', `get` returns it, and no list, review or query ever shows it."""
    start = "2026-02-30T09:00:00-05:00"
    with open_store(tmp_path / "a.db") as s:
        try:
            if kind == "session":
                p = session()
                p["time"] = {"start": start}
                [e] = commands.log(s, ME, [{"kind": "session", "payload": p}])
            else:
                payload = {"code": "body_mass", "value": 80, "unit": "kg", "time": {"start": start}}
                [e] = commands.log(s, ME, [{"kind": "observation", "payload": payload}])
        except ValidationFailed:
            return
        table = {"session": "sessions", "observation": "observations"}[kind]
        live = s.get(kind, e["record"]["id"]) is not None
        shown = s.rows(f"SELECT id FROM {table} WHERE id = ?", (e["record"]["id"],))
    assert live and shown, f"the {kind} was accepted and is live, but no derived table holds it"


def test_editing_a_session_to_an_impossible_date_does_not_make_it_vanish(tmp_path):
    with open_store(tmp_path / "a.db") as s:
        [e] = commands.log(s, ME, [{"kind": "session", "payload": session()}])
        rid = e["record"]["id"]
        try:
            commands.edit(s, ME, "session", rid, {"time": {"start": "2026-02-29T09:00:00-05:00", "end": None}})
        except ValidationFailed:
            pass
        shown = s.rows("SELECT id FROM sessions WHERE id = ?", (rid,))
        occurrence = s.rows("SELECT id FROM occurrence_items WHERE id = ?", (rid,))
    assert shown and occurrence, "an accepted edit with a date that doesn't exist removed the session from reads"


def test_an_evening_session_written_in_utc_lands_on_its_home_zone_day(tmp_path):
    store = open_store(tmp_path / "a.db")
    commands.log(store, ME, [{"kind": "setting", "id": "home_zone", "payload": {"value": "America/New_York"}}])
    # 8 pm on Nov 19 in New York, written by an agent in UTC.
    [rec] = commands.log(store, ME, [{"kind": "session", "payload": {
        "name": "lift", "time": {"start": "2026-11-20T01:00:00Z", "end": "2026-11-20T02:00:00Z"}}}])
    day = store.rows("SELECT local_date FROM sessions WHERE id = ?", (rec["record"]["id"],))[0]["local_date"]
    assert day == "2026-11-19"


def _grams(source_id):
    return {"source": "apple_health", "source_id": source_id, "type": "HKQuantityTypeIdentifierBodyMass",
            "value": 80000, "unit": "g", "start": "2026-03-02T07:00:00-05:00", "end": "2026-03-02T07:00:00-05:00"}


def test_a_unit_learned_on_upgrade_converts_imported_observations_too(tmp_path):
    """`use()` re-derives authored kinds when the registry's units change, but not source records, whose
    observations and workout metrics carry `std_value` just the same. They keep the old answer until a manual
    `byobot rebuild`, and every read in the code's own unit (review, the views, agents' queries) misses them."""
    path = tmp_path / "a.db"
    with open_store(path) as s:
        relay.accept_samples(s, {"samples": [_grams("g-1")], "deleted": []})
        registry = copy.deepcopy(s.contract.registry)
        assert s.rows("SELECT std_value FROM observations") == [{"std_value": None}]
    registry["codes"]["body_mass"]["units"]["g"] = 0.001  # a later release accepts grams
    upgraded = Contract(registry)
    with open_store(path, contract=upgraded) as s:  # the upgrade's first open runs use()
        after_open = s.rows("SELECT std_value FROM observations")
        s.rebuild()
        after_rebuild = s.rows("SELECT std_value FROM observations")
    assert after_rebuild == [{"std_value": 80.0}]
    assert after_open == after_rebuild, "the upgrade left the imported weight unconverted"
