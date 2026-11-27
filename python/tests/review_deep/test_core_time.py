"""Deep review (core): times, days and the clock."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _core_helpers import ME, open_store, session  # noqa: E402

from byobot.core import commands  # noqa: E402
from byobot.util import ValidationFailed  # noqa: E402


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
                e = commands.observe(s, ME, "body_mass", 80, "kg", start=start)
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
