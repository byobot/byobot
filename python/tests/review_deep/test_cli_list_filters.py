"""Deep review (cli): `list --since/--until` either filters by the time asked for or refuses the value.

`_list` only parses a bound that isn't 10 characters long, and for `list events` never parses it at all: a bound is
compared with the stored text. A 10-character value that isn't YYYY-MM-DD (2026/11/20, 11/20/2026) is compared as
text and quietly returns nothing or everything, and an events bound with an offset is compared as text against
`ts`, which each device writes in its own local offset.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import via_cli, via_mcp  # noqa: E402

from byobot.commands import Context, run  # noqa: E402
from byobot.core import commands as core  # noqa: E402
from byobot.core.store import Store  # noqa: E402

ME = {"kind": "human", "id": "me"}
EST = timezone(timedelta(hours=-5))


@pytest.mark.parametrize("bound", ["2026/11/20", "11/20/2026", "20.11.2026", "garbage123"])
@pytest.mark.parametrize("which", ["since", "until"])
def test_cli_a_date_bound_that_is_not_a_date_is_refused(bound, which):
    """CLI-4: one session on 2026-11-25; `--since 2026/11/20` lists none and `--since 11/20/2026` lists it, both
    without a word, so an agent reports "no sessions" or the wrong ones."""
    assert via_cli(["log", "session", '{"name": "Example session", "time": {"start": "2026-11-25T10:00:00-05:00"}}'])[0] \
        == "ok"
    assert via_cli(["list", "sessions", f"--{which}", bound]) == ("error", "invalid")
    assert via_mcp("list", {"kind": "observations", which: bound}) == ("error", "invalid")


@pytest.mark.parametrize("bound", ["yesterday", "2026-13-45T99:00:00Z"])
def test_cli_an_events_bound_that_is_not_a_time_is_refused(bound):
    """CLI-4: `list events` never checks its bounds."""
    assert via_cli(["init"])[0] == "ok"
    assert via_cli(["list", "events", "--since", bound]) == ("error", "invalid")


def test_cli_events_since_a_timestamp_compares_instants_not_text(byobot_home):
    """CLI-4: an event written at 10:00-05:00 (15:00Z) is after 14:00Z and before 16:00Z, but `ts >= '…14:00:00Z'`
    compares "10:00…" with "14:00…" as text: it is left out of `--since 14:00Z` and kept by `--until 12:00Z`."""
    with Store(byobot_home / "byobot.db", wall=lambda: datetime(2026, 11, 20, 10, 0, tzinfo=EST)) as store:
        core.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    ctx = Context(actor=ME, path=byobot_home / "byobot.db")
    try:
        since = run("list", ctx, {"kind": "events", "type": "location", "since": "2026-11-20T14:00:00Z"})
        until = run("list", ctx, {"kind": "events", "type": "location", "until": "2026-11-20T12:00:00Z"})
    finally:
        ctx.close()
    assert (since["count"], until["count"]) == (1, 0)
