"""Correctness review: core store and commands."""

from datetime import datetime, timedelta, timezone

import pytest

from byobot.core import commands
from byobot.core.store import Store

ME = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "example-agent"}


class Wall:
    def __init__(self, start=datetime(2026, 3, 2, 15, tzinfo=timezone.utc)):
        self.at = start

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


def session(start="2026-03-02T09:00:00-05:00", end="2026-03-02T10:00:00-05:00", name="lift"):
    return {"name": name, "time": {"start": start, "end": end}}


def effort(sid, reps=5):
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


def _effort(reps=5):
    return {"activity_id": "row", "time": {"start": "2026-03-02T10:00:00-05:00"},
            "metrics": {"reps": {"value": reps}}}


def test_erasing_a_session_with_efforts_keeps_an_effort_moved_to_another_session(one):
    [a] = commands.log(one, ME, [{"kind": "session", "payload": session(name="a")}])
    [b] = commands.log(one, ME, [{"kind": "session", "payload": session(name="b")}])
    sa, sb = a["record"]["id"], b["record"]["id"]
    [e] = commands.log(one, ME, [{"kind": "effort", "payload": effort(sa)}])
    rid = e["record"]["id"]
    commands.edit(one, ME, "effort", rid, {"links": {"part_of": {"kind": "session", "id": sb}}})
    commands.erase(one, ME, "session", sa, with_efforts=True)
    assert one.get("effort", rid) is not None, "an effort now in session b was erased with session a"



def test_undoing_a_resolve_brings_the_conflict_back(tmp_path):
    a = Store(tmp_path / "a.db", device_id="phone", wall=Wall())
    b = Store(tmp_path / "b.db", device_id="laptop", wall=Wall())
    b.adopt(a.store_id, a._get("store_key"))
    [e] = commands.log(a, ME, [{"kind": "effort", "payload": _effort()}])
    rid = e["record"]["id"]
    b.accept_many(a.events())
    six = commands.edit(a, ME, "effort", rid, {"metrics": {"reps": {"value": 6}}})
    commands.edit(b, AGENT, "effort", rid, {"metrics": {"reps": {"value": 7}}})
    a.accept_many(b.events())
    fixed = commands.resolve(a, ME, "effort", rid, six["id"])
    commands.undo(a, ME, fixed["id"])
    assert a.conflicts("effort", rid), "undo of a resolve kept only one side; the other version is no longer shown"
