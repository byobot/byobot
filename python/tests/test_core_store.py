"""The core store and its commands, on small hand-made histories."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from byobot.core import commands
from byobot.core.contract import Contract
from byobot.core.store import Store
from byobot.util import Conflict, NotFound

ME = {"kind": "human", "id": "me"}
T0 = datetime(2026, 3, 2, 10, tzinfo=timezone(timedelta(hours=-5)))


class Wall:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


def effort(reps=5):
    return {"activity_id": "row", "time": {"start": "2026-03-02T10:00:00-05:00"}, "modality": "self_reported",
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
    assert sqlite3.connect(a.path).execute("PRAGMA user_version").fetchone()[0] == 5


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
    commands.undo(a, ME, e["id"])
    assert a.get("effort", rid) is None
    assert a.conn.execute("SELECT count(*) FROM efforts").fetchone()[0] == 0


def test_undo_takes_back_a_whole_batch(pair):
    a, _ = pair
    session = {"time": {"start": "2026-03-02T10:00:00-05:00"}, "modality": "self_reported"}
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
    b.use(Contract.shipped().newer(2, {"meal": {"category": "act", "version": 2, "description": "A meal."}}))
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
