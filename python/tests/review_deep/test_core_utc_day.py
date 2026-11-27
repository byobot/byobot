"""An agent that writes a time in UTC ("Z") for an evening session puts it on the next day, even with home_zone set."""

from byobot.core import commands
from tests.review_deep._core_helpers import ME, open_store


def test_an_evening_session_written_in_utc_lands_on_its_home_zone_day(tmp_path):
    store = open_store(tmp_path / "a.db")
    commands.log(store, ME, [{"kind": "setting", "id": "home_zone", "payload": {"value": "America/New_York"}}])
    # 8 pm on Nov 19 in New York, written by an agent in UTC.
    [rec] = commands.log(store, ME, [{"kind": "session", "payload": {
        "name": "lift", "time": {"start": "2026-11-20T01:00:00Z", "end": "2026-11-20T02:00:00Z"}}}])
    day = store.rows("SELECT local_date FROM sessions WHERE id = ?", (rec["record"]["id"],))[0]["local_date"]
    assert day == "2026-11-19"
