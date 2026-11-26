"""A write names the version it was made from, so nothing it didn't see is overwritten or chosen."""

from datetime import datetime, timedelta, timezone

import pytest

from byobot.commands import Context, run
from byobot.core import commands, reducers
from byobot.core.contract import Contract
from byobot.core.store import Store
from byobot.util import Conflict

ME = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "example-agent"}


class Wall:
    def __init__(self):
        self.at = datetime(2026, 3, 2, 15, tzinfo=timezone.utc)

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


def effort(reps=5):
    return {"activity_id": "row", "time": {"start": "2026-03-02T10:00:00-05:00"}, "modality": "self_reported",
            "metrics": {"reps": {"value": reps}}}


def reps(store, rid):
    return store.get("effort", rid)["payload"]["metrics"]["reps"]["value"]


def sync(*stores):
    for a in stores:
        for b in stores:
            if a is not b:
                b.accept_many(a.events())


@pytest.fixture
def stores(tmp_path):
    made = [Store(tmp_path / f"{n}.db", device_id=n, wall=Wall()) for n in ("phone", "laptop", "tablet")]
    for s in made[1:]:
        s.adopt(made[0].store_id, made[0]._get("store_key"))
    yield made
    for s in made:
        s.close()


def conflicted(a, b):
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    sync(a, b)
    commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    commands.edit(b, AGENT, "effort", rid, {"metrics": {"reps": {"value": 7}}})
    sync(a, b)
    assert a.conflicts()
    return rid


def test_an_edit_of_a_conflicted_record_keeps_the_conflict(stores):
    a, b, _ = stores
    rid = conflicted(a, b)
    commands.edit(a, AGENT, "effort", rid, {"notes": "felt easy"})
    [found] = a.conflicts()
    assert sorted(v["payload"]["metrics"]["reps"]["value"] for v in found["versions"]) == [6, 7]


def test_removing_a_conflicted_record_keeps_the_conflict(stores):
    a, b, _ = stores
    rid = conflicted(a, b)
    commands.remove(a, AGENT, "effort", rid)
    assert a.conflicts()


def test_a_push_between_read_and_write_is_a_conflict(stores):
    a, b, _ = stores
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    sync(a, b)
    commands.edit(b, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    real = a.write

    def racing(actor, changes):
        a.accept_many(b.events())  # the phone's push lands after the edit read
        return real(actor, changes)

    a.write = racing
    commands.edit(a, AGENT, "effort", rid, {"notes": "felt easy"})
    a.write = real
    assert a.conflicts() and 6 in [v["payload"]["metrics"]["reps"]["value"] for v in a.conflicts()[0]["versions"]]


def test_resolve_keeps_a_version_that_arrived_after_the_person_looked(stores):
    a, b, c = stores
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": effort()}])
    rid = e["record"]["id"]
    sync(a, b, c)
    commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    commands.edit(b, ME, "effort", rid, {"metrics": {"reps": {"value": 7}}})
    b.accept_many(a.events())
    [shown] = b.conflicts()
    seen = [v["id"] for v in shown["versions"]]
    commands.edit(c, ME, "effort", rid, {"metrics": {"reps": {"value": 8}}})
    b.accept_many(c.events())
    commands.resolve(b, ME, "effort", rid, seen[0], seen)
    [left] = b.conflicts()
    assert 8 in [v["payload"]["metrics"]["reps"]["value"] for v in left["versions"]]
    commands.resolve(b, ME, "effort", rid, left["versions"][0]["id"])
    assert not b.conflicts()


def adds(field):
    """A contract a version ahead, whose effort version 2 added `field`."""
    spec = Contract.shipped().registry["kinds"]["effort"]
    return Contract.shipped().newer(2, {"effort": {**spec, "version": 2, "since": {field: 2}}})


def test_a_writer_writes_the_lowest_version_that_holds_the_payload(tmp_path):
    old = Store(tmp_path / "old.db", device_id="laptop", wall=Wall())
    new = Store(tmp_path / "new.db", device_id="phone", wall=Wall(), contract=adds("tempo"))
    [plain, fast] = commands.log(new, ME, [{"kind": "effort", "payload": effort(5)},
                                           {"kind": "effort", "payload": {**effort(6), "tempo": "fast"}}])
    assert (plain["kind_version"], fast["kind_version"]) == (1, 2)
    reshaped = adds("tempo").newer(3, {"effort": {**adds("tempo").registry["kinds"]["effort"], "version": 3,
                                                  "reshaped": 3}})
    assert reducers.lowest(reshaped, "effort", effort(5)) == 3
    old.accept_many(new.events())
    assert reps(old, plain["record"]["id"]) == 5 and old.get("effort", fast["record"]["id"]) is None


def test_an_older_install_wont_overwrite_a_version_it_cant_read(tmp_path):
    old = Store(tmp_path / "old.db", device_id="laptop", wall=Wall())
    new = Store(tmp_path / "new.db", device_id="phone", wall=Wall(), contract=adds("metrics"))
    new.adopt(old.store_id, old._get("store_key"))
    [e] = commands.log(old, ME, [{"kind": "effort", "payload": effort(5)}])
    rid = e["record"]["id"]
    new.accept_many(old.events())
    commands.edit(new, ME, "effort", rid, {"metrics": {"reps": {"value": 8}}})
    old.accept_many(new.events())
    with pytest.raises(Conflict, match="newer byobot"):
        commands.edit(old, AGENT, "effort", rid, {"notes": "agent note"})
    assert reps(new, rid) == 8
    assert old.get("effort", rid)["newer"] and reps(old, rid) == 5 and old.counts()["newer"] == 1
    assert [(r["id"], r["event"]["id"], r["newer"]) for r in old.records("effort")] == [(rid, e["id"], True)]
    assert old.newer() == {("effort", rid)} and not new.get("effort", rid)["newer"]
    listed = run("list", Context(actor=ME, _store=old), {"kind": "efforts"})["items"]
    assert [(i["id"], i.get("newer")) for i in listed] == [(rid, True)]
    old.use(adds("metrics"))
    assert not old.get("effort", rid)["newer"] and reps(old, rid) == 8


def test_older_versions_are_lifted_before_they_are_read_or_edited(tmp_path, monkeypatch):
    lift = {("effort", 1): lambda p: {**p, "metrics": {**p["metrics"], "rpe": {"value": 8}}}}
    monkeypatch.setattr(reducers, "UPCASTS", lift)
    old = Store(tmp_path / "old.db", device_id="laptop", wall=Wall())
    [e] = commands.log(old, ME, [{"kind": "effort", "payload": effort(5)}])
    rid = e["record"]["id"]
    old.use(Contract.shipped().newer(2))
    assert old.rows("SELECT code FROM metrics WHERE owner_id = ? ORDER BY code", (rid,)) == [
        {"code": "reps"}, {"code": "rpe"}]
    edited = commands.edit(old, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    assert edited["kind_version"] == 2 and edited["payload"]["metrics"] == {"reps": {"value": 6}, "rpe": {"value": 8}}
    assert old.event(e["id"])["payload"]["metrics"] == {"reps": {"value": 5}}
