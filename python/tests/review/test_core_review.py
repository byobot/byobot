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
    return {"name": name, "time": {"start": start, "end": end}, "modality": "self_reported"}


def effort(sid, reps=5):
    return {"activity_id": "row", "time": {"start": "2026-03-02T09:10:00-05:00"}, "modality": "self_reported",
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
    return {"activity_id": "row", "time": {"start": "2026-03-02T10:00:00-05:00"}, "modality": "self_reported",
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
