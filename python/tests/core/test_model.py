"""The reference model's semantics, pinned by hand so the oracle itself is trustworthy."""

import pytest

from .model import ModelReplica, Refused

H = "human:me"


def at(minute: int, hours: float = 0) -> str:
    return f"2026-03-02T{10 + int(hours):02d}:{minute:02d}:00.000Z"


def effort(reps: int, rid: str = "e1") -> dict:
    return {"intent": "log_effort", "record": {"id": rid, "activity_id": "row", "performed_at": at(0),
                                               "metrics": {"reps": {"value": reps}}}}


def edit(reps: int, rid: str = "e1") -> dict:
    return {"intent": "edit", "record": ["effort", rid], "fields": {"metrics": {"reps": {"value": reps}}}}


def reps(replica: ModelReplica, rid: str = "e1") -> int | None:
    record = replica.snapshot()["records"].get(f"effort/{rid}")
    return record and record["fields"]["metrics"]["reps"]["value"]


def pair() -> tuple[ModelReplica, ModelReplica]:
    phone, laptop = ModelReplica("phone"), ModelReplica("laptop")
    laptop.deliver(phone.author(effort(5), at(0), H))
    return phone, laptop


def sync(a: ModelReplica, b: ModelReplica) -> None:
    a.deliver(list(b.writes.values()))
    b.deliver(list(a.writes.values()))


def test_sequential_edits_never_conflict():
    phone, laptop = pair()
    phone.author(edit(6), at(1), H)
    phone.author(edit(7), at(2), H)
    sync(phone, laptop)
    assert reps(laptop) == 7 and not laptop.snapshot()["conflicts"]


def test_concurrent_edits_conflict_and_converge():
    phone, laptop = pair()
    phone.author(edit(6), at(5), H)
    laptop.author(edit(9), at(1), H)
    sync(phone, laptop)
    assert phone.snapshot() == laptop.snapshot()
    versions = laptop.snapshot()["conflicts"]["effort/e1"]
    assert sorted(v["metrics"]["reps"]["value"] for v in versions) == [6, 9]
    assert reps(laptop) == 6  # the provisional version is the head with the highest clock


def test_identical_concurrent_edits_do_not_conflict():
    phone, laptop = pair()
    phone.author(edit(6), at(5), H)
    laptop.author(edit(6), at(1), H)
    sync(phone, laptop)
    assert not laptop.snapshot()["conflicts"] and reps(laptop) == 6


def test_a_write_made_after_seeing_another_wins_whatever_the_clocks():
    phone, laptop = pair()
    phone.author(edit(5), at(0, hours=3), H)  # phone three hours fast
    sync(phone, laptop)
    laptop.author(edit(9), at(30), H)
    sync(phone, laptop)
    assert reps(phone) == reps(laptop) == 9 and not laptop.snapshot()["conflicts"]


def test_undo_writes_the_previous_content_back():
    phone = ModelReplica("phone")
    [created] = phone.author(effort(5), at(0), H)
    [edited] = phone.author(edit(9), at(1), H)
    [undo] = phone.author({"intent": "undo", "events": [edited]}, at(2), H)
    assert reps(phone) == 5 and undo.reverts == edited.id
    phone.author({"intent": "undo", "events": [undo]}, at(3), H)
    assert reps(phone) == 9
    with pytest.raises(Refused):  # the effort changed after it was logged
        phone.author({"intent": "undo", "events": [created]}, at(4), H)
    assert reps(phone) == 9


def test_undo_racing_an_edit_is_a_conflict_in_any_order():
    phone, laptop = pair()
    [edited] = phone.author(edit(6), at(1), H)
    sync(phone, laptop)
    undo = laptop.author({"intent": "undo", "events": [edited]}, at(2), H)
    other = phone.author(edit(8), at(3), H)
    late = ModelReplica("late")
    late.deliver(undo)  # the undo before its target and before the record exists
    late.deliver(other)
    late.deliver(list(phone.writes.values()))
    sync(phone, laptop)
    assert late.snapshot() == laptop.snapshot() == phone.snapshot()
    assert "effort/e1" in laptop.snapshot()["conflicts"]


def test_resolving_clears_the_conflict_everywhere():
    phone, laptop = pair()
    phone.author(edit(6), at(5), H)
    laptop.author(edit(9), at(1), H)
    sync(phone, laptop)
    laptop.author({"intent": "resolve", "record": ["effort", "e1"], "choice": {"activity_id": "row",
                   "performed_at": at(0), "metrics": {"reps": {"value": 7}}}}, at(6), H)
    sync(phone, laptop)
    assert not phone.snapshot()["conflicts"] and reps(phone) == 7


def test_two_different_resolutions_conflict_again_and_converge():
    phone, laptop = pair()
    phone.author(edit(6), at(5), H)
    laptop.author(edit(9), at(1), H)
    sync(phone, laptop)
    phone.author({"intent": "resolve", "record": ["effort", "e1"], "choice": 0}, at(7), H)
    laptop.author({"intent": "resolve", "record": ["effort", "e1"], "choice": 1}, at(7), H)
    sync(phone, laptop)
    assert phone.snapshot() == laptop.snapshot() and "effort/e1" in phone.snapshot()["conflicts"]


def test_logging_a_taken_id_is_refused():
    phone, _ = pair()
    with pytest.raises(Refused):
        phone.author(effort(1), at(3), H)
    assert reps(phone) == 5


def test_erase_wins_and_redacts_later_writes():
    phone, laptop = pair()
    late = laptop.author(edit(9), at(1), H)
    phone.author({"intent": "erase", "record": ["effort", "e1"]}, at(2), H)
    phone.deliver(late)
    snap = phone.snapshot()
    assert "effort/e1" not in snap["records"] and snap["erased"] == ["effort/e1"] and not snap["conflicts"]


def test_inline_efforts_are_records_of_their_own():
    phone = ModelReplica("phone")
    phone.author({"intent": "log_session", "record": {"id": "s1", "started_at": at(0), "efforts": [
        {"id": "e1", "activity_id": "row"}]}}, at(0), H)
    assert phone.snapshot()["records"]["effort/e1"]["fields"] == {
        "activity_id": "row", "performed_at": at(0), "session_id": "s1"}


def test_replanning_keeps_planned_effort_ids_by_position():
    phone = ModelReplica("phone")
    plan = {"id": "p1", "scheduled_on": "2026-03-02", "name": "Legs",
            "blocks": [{"id": "b1", "efforts": [{"id": "pe1", "activity_id": "squat"}]}]}
    phone.author({"intent": "plan_session", "record": plan}, at(0), H)
    phone.author({"intent": "replan_session", "record": {**plan, "blocks": [
        {"efforts": [{"activity_id": "squat", "metrics": {"reps": {"value": 3}}}]}]}}, at(1), H)
    block = phone.snapshot()["records"]["planned_session/p1"]["fields"]["blocks"][0]
    assert block["id"] == "b1" and block["efforts"][0]["id"] == "pe1"


def workout(wid: str, start: str, end: str, updated: str, distance: float | None = None) -> dict:
    score = {"average_heart_rate": 150, "max_heart_rate": 170, "kilojoule": 2000.0, "percent_recorded": 100.0,
             "strain": 12.0, "zone_durations": {}}
    if distance is not None:
        score["distance_meter"] = distance
    return {"id": wid, "user_id": 1, "created_at": start, "updated_at": updated, "start": start, "end": end,
            "timezone_offset": "+00:00", "sport_name": "running", "score_state": "SCORED", "score": score}


def health_workout(sid: str, start: str, end: str, distance: float) -> dict:
    return {"source": "apple-health", "source_id": sid, "type": "HKWorkoutTypeIdentifier", "start": start,
            "end": end, "value": "running", "raw": {"distance_m": distance, "heart_rate_avg_bpm": 148}}


def test_source_revisions_are_all_kept_and_reimport_is_a_no_op():
    phone = ModelReplica("phone")
    first = workout("w1", at(0), at(50), at(51))
    assert len(phone.ingest("whoop", {"workout": [first]}, at(52))) == 1
    assert phone.ingest("whoop", {"workout": [first]}, at(53)) == []
    phone.ingest("whoop", {"workout": [{**first, "updated_at": at(59), "score": {**first["score"], "strain": 13.0}}]},
                 at(59))
    history = phone.source_history("whoop", "workout", "w1")
    assert [h["score"]["strain"] for h in history] == [12.0, 13.0]


def test_two_devices_importing_one_revision_write_one_event():
    phone, laptop = ModelReplica("phone"), ModelReplica("laptop")
    w = workout("w1", at(0), at(50), at(51))
    sent = phone.ingest("whoop", {"workout": [w]}, at(52))
    laptop.ingest("whoop", {"workout": [w]}, at(55))
    assert laptop.deliver(sent) == [("duplicate", "")]


def test_one_workout_from_three_sources_is_one_occurrence_with_gaps_filled():
    phone = ModelReplica("phone")
    phone.author({"intent": "log_session", "record": {"id": "s1", "started_at": "2026-03-02T10:00:00.000Z",
                  "ended_at": "2026-03-02T11:00:00.000Z"}}, at(0), H)
    phone.ingest("whoop", {"workout": [workout("w1", "2026-03-02T10:02:00.000Z", "2026-03-02T10:58:00.000Z",
                                               at(59))]}, at(59))
    phone.ingest("apple-health", [health_workout("h1", "2026-03-02T10:01:00.000Z", "2026-03-02T10:59:00.000Z",
                                                 9000)], at(59))
    [o] = phone.occurrences()
    assert o["start"].startswith("2026-03-02T10:00:00")
    assert o["metrics"] == {"duration_s": "3360", "distance_m": "9000", "heart_rate": "150"}
    assert o["provenance"] == {"duration_s": "whoop", "distance_m": "apple-health", "heart_rate": "whoop"}


def test_back_to_back_workouts_overlapping_a_minute_stay_two():
    phone = ModelReplica("phone")
    phone.ingest("whoop", {"workout": [
        workout("w1", "2026-03-02T10:00:00.000Z", "2026-03-02T10:31:00.000Z", at(59)),
        workout("w2", "2026-03-02T10:30:00.000Z", "2026-03-02T11:00:00.000Z", at(59))]}, at(59))
    assert len(phone.occurrences()) == 2


def test_a_correction_survives_a_rescore():
    phone = ModelReplica("phone")
    w = workout("w1", "2026-03-02T10:00:00.000Z", "2026-03-02T11:00:00.000Z", at(1), distance=5000)
    phone.ingest("whoop", {"workout": [w]}, at(2))
    phone.author({"intent": "correct_import", "source": "whoop", "source_id": "w1",
                  "fields": {"distance_m": 10000}}, at(3), H)
    phone.ingest("whoop", {"workout": [{**w, "updated_at": at(9), "score": {**w["score"], "strain": 14.0}}]}, at(9))
    [o] = phone.occurrences()
    assert o["metrics"]["distance_m"] == "10000" and o["provenance"]["distance_m"] == "you"


def test_local_dates_come_from_the_written_offset():
    phone = ModelReplica("phone")
    phone.author({"intent": "log_effort", "record": {"id": "e1", "activity_id": "row",
                  "performed_at": "2026-03-02T23:30:00.000-05:00"}}, at(0), H)
    assert phone.local_dates() == {"effort/e1": "2026-03-02"}
