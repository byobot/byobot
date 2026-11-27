"""Deep review (cli): `byobot review`'s plan adherence reads a planned session's status.

A planned session's status is planned, skipped or cancelled. review() leaves out only "skipped": a cancelled
session still counts as planned and as missed.
"""

from byobot.core import commands as core
from byobot.core.store import Store
from byobot.review import review

ME = {"kind": "human", "id": "me"}


def _planned(day: str, status: str | None) -> dict:
    payload = {"scheduled_on": day, "name": f"Example {status or 'planned'} day", "location_id": "example_gym",
               "blocks": [{"efforts": [{"activity_id": "x.example_press"}]}]}
    if status:
        payload["status"] = status
    return {"kind": "planned_session", "payload": payload}


def test_cli_a_cancelled_session_is_not_planned_or_missed(byobot_home):
    """CLI-12: an agent that cancels Tuesday's session (status cancelled) then reads `review` is told the person
    missed it, and adherence counts it as planned."""
    with Store(byobot_home / "byobot.db") as store:
        core.log(store, ME, [_planned("2026-11-17", "cancelled"), _planned("2026-11-18", None),
                             _planned("2026-11-19", "skipped")])
        out = review(store, 2, "2026-11-23")
    assert out["plan_adherence"]["planned"] == 1
    assert [m["scheduled_on"] for m in out["plan_adherence"]["missed"]] == ["2026-11-18"]
