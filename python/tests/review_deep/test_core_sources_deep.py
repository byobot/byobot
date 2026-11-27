"""Deep review (core): the source lane keeps what a source gave, whatever it reads as."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _core_helpers import ME, open_store, whoop_workout  # noqa: E402

from byobot.core import commands, relay, sources  # noqa: E402


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
