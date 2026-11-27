"""S7 to S10: sources and merging."""

from itertools import permutations

import pytest

from .scenarios import both_ways, effort, health_workout, session, weight, whoop_sleep, whoop_workout
from .snapshot import diff

T = "2026-03-02T{}:00.000Z".format
SLEEP = whoop_sleep("8f3c2a10-0000-4000-8000-000000000001", 1001, T("00:00"), T("07:30"), T("08:00"))


# -- S7. a source revises or deletes old records -----------------------------------------------------


def test_s7_a_rescored_night_keeps_both_revisions(world):
    world.replica("laptop")
    world.ingest("laptop", "whoop", {"sleep": [SLEEP]})
    world.tick(3 * 86400)
    world.ingest("laptop", "whoop", {"sleep": [{**SLEEP, "updated_at": "2026-03-05T08:00:00.000Z",
                                                "score": {**SLEEP["score"], "sleep_performance_percentage": 64}}]})
    history = world.source_history("laptop", "whoop", SLEEP["id"])
    assert [h["score"]["sleep_performance_percentage"] for h in history] == [80, 64]


def test_s7_a_corrected_weight_keeps_both_readings(world):
    world.replica("phone")
    world.ingest("phone", "apple-health", [weight("w-1", T("07:00"), 80.4, T("07:00"))])
    world.ingest("phone", "apple-health", [weight("w-1", T("07:00"), 80.1, T("09:00"))])
    assert [h["value"] for h in world.source_history("phone", "apple-health", "w-1")] == [80.4, 80.1]


def test_s7_an_older_revision_arriving_later_stays_older(world):
    world.replica("phone")
    world.ingest("phone", "apple-health", [weight("w-1", T("07:00"), 80.1, T("09:00"))])
    world.ingest("phone", "apple-health", [weight("w-1", T("07:00"), 80.4, T("07:00"))])
    assert [h["value"] for h in world.source_history("phone", "apple-health", "w-1")] == [80.4, 80.1]
    assert diff(world.snapshot("phone"), world.expected("phone")) == []


def test_s7_resending_identical_data_changes_nothing(world):
    world.replica("laptop")
    batch = {"sleep": [SLEEP], "workout": [whoop_workout("9a1b0000-0000-4000-8000-000000000001", T("10:00"),
                                                         T("10:50"), T("11:00"))]}
    world.ingest("laptop", "whoop", batch)
    before = world.log_digest("laptop"), world.snapshot("laptop"), world.occurrences("laptop")
    world.ingest("laptop", "whoop", batch)
    assert (world.log_digest("laptop"), world.snapshot("laptop"), world.occurrences("laptop")) == before


def test_s7_a_workout_deleted_at_the_source_leaves_the_reads(world):
    world.replica("phone")
    world.ingest("phone", "apple-health", [health_workout("h1", T("10:00"), T("10:45"), 7000)])
    world.ingest("phone", "apple-health", {"samples": [], "deleted": [("HKWorkoutTypeIdentifier", "h1")]})
    assert world.occurrences("phone") == world.expected_occurrences("phone") == []


# -- S8. the same workout from WHOOP, Apple Health and manual logging ----------------------------------

ORDERS = list(permutations(("log", "whoop", "apple-health")))
WID = "9a1b0000-0000-4000-8000-000000000002"


def _arrive(world, step):
    if step == "log":
        world.author("phone", session("s1", T("10:00"), T("11:00")))
        world.sync("phone", "laptop")
    elif step == "whoop":
        world.ingest("laptop", "whoop", {"workout": [whoop_workout(WID, T("10:02"), T("10:58"), T("11:05"))]})
    else:
        world.ingest("laptop", "apple-health", [health_workout("h1", T("10:01"), T("10:59"), 9000)])


@pytest.mark.parametrize("order", ORDERS, ids="-".join)
def test_s8_one_workout_from_three_sources_is_one_session_with_gaps_filled(world, order):
    world.replica("phone")
    world.replica("laptop")
    for step in order:
        _arrive(world, step)
        world.tick(600)
    both_ways(world, "phone", "laptop")
    assert world.occurrences("laptop") == world.expected_occurrences("laptop")
    [one] = world.occurrences("laptop")
    assert one["metrics"]["distance_m"] == "9000"


def test_s8_the_log_is_the_same_whatever_order_connectors_ran_in(driver, tmp_path):
    from .conftest import DRIVERS
    from .harness import World

    digests = set()
    for i, order in enumerate(ORDERS):
        world = World(DRIVERS[driver](), tmp_path / str(i))
        (tmp_path / str(i)).mkdir()
        world.replica("phone")
        world.replica("laptop")
        for step in order:
            _arrive(world, step)
        both_ways(world, "phone", "laptop")
        digests.add(tuple(world.log_digest("laptop")))
        world.close()
    assert len(digests) == 1


@pytest.mark.parametrize("second", [
    pytest.param("whoop", id="both_whoop"),
    pytest.param("apple-health", id="whoop_then_apple_health"),
])
def test_s8_back_to_back_runs_overlapping_a_minute_stay_two(world, second):
    world.replica("laptop")
    world.ingest("laptop", "whoop", {"workout": [
        whoop_workout("9a1b0000-0000-4000-8000-000000000003", T("10:00"), T("10:31"), T("11:05"))]})
    if second == "whoop":
        world.ingest("laptop", "whoop", {"workout": [
            whoop_workout("9a1b0000-0000-4000-8000-000000000004", T("10:30"), T("11:00"), T("11:05"))]})
    else:
        world.ingest("laptop", "apple-health", [health_workout("h2", T("10:30"), T("11:00"), 5000)])
    assert len(world.occurrences("laptop")) == 2
    assert world.occurrences("laptop") == world.expected_occurrences("laptop")


def test_s8_a_correction_survives_a_rescore(world):
    world.replica("laptop")
    w = whoop_workout(WID, T("10:00"), T("11:00"), T("11:05"), distance=5000)
    world.ingest("laptop", "whoop", {"workout": [w]})
    world.author("laptop", {"intent": "correct_import", "source": "whoop", "source_id": WID,
                            "fields": {"distance_m": 10000}})
    rescored = {**w, "updated_at": T("15:00"), "score": {**w["score"], "strain": 14.0}}
    world.ingest("laptop", "whoop", {"workout": [rescored]})
    [one] = world.occurrences("laptop")
    assert one["metrics"]["distance_m"] == "10000"
    assert world.occurrences("laptop") == world.expected_occurrences("laptop")


def test_s8_a_correction_shows_before_any_rescore(world):
    world.replica("laptop")
    world.ingest("laptop", "whoop", {"workout": [whoop_workout(WID, T("10:00"), T("11:00"), T("11:05"), distance=5000)]})
    world.author("laptop", {"intent": "correct_import", "source": "whoop", "source_id": WID,
                            "fields": {"distance_m": 10000}})
    [one] = world.occurrences("laptop")
    assert one["metrics"]["distance_m"] == "10000"
    assert world.occurrences("laptop") == world.expected_occurrences("laptop")


def test_s8_two_of_your_own_overlapping_sessions_stay_two(world):
    world.replica("phone")
    world.author("phone", session("s1", T("10:00"), T("11:00")))
    world.author("phone", session("s2", T("10:15"), T("10:45")))
    assert len(world.occurrences("phone")) == 2
    assert world.occurrences("phone") == world.expected_occurrences("phone")


def test_s8_an_import_after_undoing_your_session_still_counts_once(world):
    world.replica("laptop")
    logged = world.author("laptop", session("s1", T("10:00"), T("11:00")))
    world.author("laptop", {"intent": "undo", "change": logged.id})
    world.ingest("laptop", "whoop", {"workout": [whoop_workout(WID, T("10:02"), T("10:58"), T("11:05"))]})
    assert len(world.occurrences("laptop")) == 1


# -- S9. identity collisions ---------------------------------------------------------------------------


def test_s9_an_agent_reusing_an_id_is_refused(world):
    world.replica("laptop")
    world.author("laptop", effort("e1", reps=5))
    change = world.author("laptop", {**effort("e1", reps=12, activity="plank")},
                          {"kind": "agent", "id": "example-agent"})
    assert change.refused
    assert diff(world.snapshot("laptop"), world.expected("laptop")) == []


def test_s9_two_devices_defining_one_activity_apart_conflict(world):
    world.replica("phone")
    world.replica("laptop")
    world.author("phone", {"intent": "define_activity", "record": {"id": "x.row", "name": "Row"}})
    world.author("laptop", {"intent": "define_activity", "record": {"id": "x.row", "name": "Rowing machine"}})
    both_ways(world, "phone", "laptop")
    assert list(world.snapshot("laptop")["conflicts"]) == ["activity/x.row"]
    for name in ("phone", "laptop"):
        assert diff(world.snapshot(name), world.expected(name)) == []


def test_s9_two_devices_importing_the_same_data_write_it_once(world):
    world.replica("phone")
    world.replica("laptop")
    batch = {"workout": [whoop_workout(WID, T("10:00"), T("11:00"), T("11:05"))]}
    world.ingest("phone", "whoop", batch)
    world.ingest("laptop", "whoop", batch)
    outcomes = world.sync("phone", "laptop")
    assert {o for o, _ in outcomes} <= {"duplicate"}
    assert world.log_digest("phone") == world.log_digest("laptop")


# -- S10. re-planning orphans logged sets ---------------------------------------------------------------

PLAN = {"id": "ps1", "scheduled_on": "2026-03-02", "name": "Legs", "blocks": [
    {"id": "ps1.b0", "rounds": 3, "efforts": [{"id": "ps1.pe0", "activity_id": "x.barbell_back_squat"}]},
    {"id": "ps1.b1", "rounds": 3, "efforts": [{"id": "ps1.pe1", "activity_id": "x.lunge"}]}]}


def test_s10_replanning_without_ids_keeps_logged_sets_linked(world):
    world.replica("phone")
    for activity in ("x.barbell_back_squat", "x.lunge"):
        world.author("phone", {"intent": "define_activity", "record": {"id": activity, "name": activity}})
    world.author("phone", {"intent": "plan_session", "record": PLAN}, {"kind": "agent", "id": "example-agent"})
    world.author("phone", {"intent": "log_effort", "record": {
        "id": "e1", "activity_id": "x.barbell_back_squat", "planned_effort_id": "ps1.pe0", "round": 1,
        "performed_at": T("10:05"), "metrics": {"reps": {"value": 5}}}})
    replanned = {**PLAN, "blocks": [{"rounds": 4, "efforts": [{"activity_id": "x.barbell_back_squat"}]},
                                    {"rounds": 3, "efforts": [{"activity_id": "x.lunge"}]}]}
    world.author("phone", {"intent": "replan_session", "record": replanned}, {"kind": "agent", "id": "example-agent"})
    plan = world.snapshot("phone")["records"]["planned_session/ps1"]["fields"]
    planned = {e.get("id") for b in plan["blocks"] for e in b["efforts"]}
    assert "ps1.pe0" in planned
    assert diff(world.snapshot("phone"), world.expected("phone")) == []


def test_s10_a_swapped_exercise_gets_a_new_planned_effort(world):
    """A replan that puts lunges where squats were must not relink the squats logged to that slot."""
    world.replica("phone")
    for activity in ("x.barbell_back_squat", "x.lunge"):
        world.author("phone", {"intent": "define_activity", "record": {"id": activity, "name": activity}})
    world.author("phone", {"intent": "plan_session", "record": PLAN}, {"kind": "agent", "id": "example-agent"})
    swapped = {**PLAN, "blocks": [{"rounds": 3, "efforts": [{"activity_id": "x.lunge"}]},
                                  {"rounds": 3, "efforts": [{"activity_id": "x.lunge"}]}]}
    world.author("phone", {"intent": "replan_session", "record": swapped}, {"kind": "agent", "id": "example-agent"})
    plan = world.snapshot("phone")["records"]["planned_session/ps1"]["fields"]
    ids = [b["efforts"][0]["id"] for b in plan["blocks"]]
    assert ids[0] != "ps1.pe0" and ids[1] == "ps1.pe1" and len(set(ids)) == 2
    assert diff(world.snapshot("phone"), world.expected("phone")) == []


def test_s10_a_reordered_or_inserted_exercise_keeps_its_logged_sets(world):
    world.replica("phone")
    for activity in ("x.barbell_back_squat", "x.lunge", "x.plank"):
        world.author("phone", {"intent": "define_activity", "record": {"id": activity, "name": activity}})
    world.author("phone", {"intent": "plan_session", "record": PLAN}, {"kind": "agent", "id": "example-agent"})
    world.author("phone", {"intent": "log_effort", "record": {
        "id": "e1", "activity_id": "x.lunge", "planned_effort_id": "ps1.pe1", "round": 1,
        "performed_at": T("10:05"), "metrics": {"reps": {"value": 8}}}})
    replanned = {**PLAN, "blocks": [{"rounds": 2, "efforts": [{"activity_id": "x.plank"}]},
                                    {"rounds": 3, "efforts": [{"activity_id": "x.lunge"}]},
                                    {"rounds": 3, "efforts": [{"activity_id": "x.barbell_back_squat"}]}]}
    world.author("phone", {"intent": "replan_session", "record": replanned}, {"kind": "agent", "id": "example-agent"})
    plan = world.snapshot("phone")["records"]["planned_session/ps1"]["fields"]
    ids = [b["efforts"][0]["id"] for b in plan["blocks"]]
    assert ids[1:] == ["ps1.pe1", "ps1.pe0"] and ids[0] not in ids[1:]
    assert diff(world.snapshot("phone"), world.expected("phone")) == []
