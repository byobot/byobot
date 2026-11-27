"""Review finding: a same_as on a source workout that also overlaps another of your sessions merges two of your
own sessions, which occurrences.py promises never happens."""

from byobot.core import commands, sources

ME = {"kind": "human", "id": "me"}
WORKOUT = "00000000-0000-4000-8000-0000000000c1"


def log_session(store, name, start, end):
    [e] = commands.log(store, ME, [{"kind": "session",
                                    "payload": {"name": name, "time": {"start": start, "end": end}}}])
    return e["record"]["id"]


def test_two_of_your_sessions_stay_two_workouts_after_a_same_as(store):
    strength = log_session(store, "Strength", "2026-11-20T09:00:00Z", "2026-11-20T10:00:00Z")
    conditioning = log_session(store, "Conditioning", "2026-11-20T10:00:00Z", "2026-11-20T10:30:00Z")
    # WHOOP recorded both as one continuous workout.
    sources.ingest(store, "whoop", [("workout", WORKOUT, {
        "id": WORKOUT, "start": "2026-11-20T09:00:00.000Z", "end": "2026-11-20T10:30:00.000Z",
        "timezone_offset": "+00:00", "sport_name": "functional-fitness", "updated_at": "2026-11-20T11:00:00.000Z",
        "score_state": "SCORED", "score": {"average_heart_rate": 140}})])
    rid = sources.record_id(store, "whoop", "workout", WORKOUT)
    assert store.rows("SELECT count(*) AS n FROM occurrences")[0]["n"] == 2
    # You say the WHOOP workout is your conditioning session.
    commands.same_as(store, ME, rid, "session", conditioning)
    homes = {r["id"]: r["occurrence_id"] for r in store.rows("SELECT id, occurrence_id FROM occurrence_items")}
    assert homes[strength] != homes[conditioning], homes
