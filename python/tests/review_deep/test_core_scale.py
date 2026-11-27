"""Deep review (core): a write's cost must not grow with history (the store's own promise, and S15's)."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _core_helpers import EST, ME, open_store, session  # noqa: E402

from byobot.core import commands  # noqa: E402

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
