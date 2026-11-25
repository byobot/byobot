"""S19 and S20: restoring from an export, and erasing."""

from pathlib import Path

import pytest

from byobot.core.store import Store

from .scenarios import both_ways, edit, effort, reps, whoop_workout
from .snapshot import diff

MARK = "zq-erase-mark-{}".format


def noted(rid: str, mark: str, reps: int = 5) -> dict:
    intent = effort(rid, reps)
    intent["record"]["notes"] = mark
    return intent


def renote(rid: str, mark: str) -> dict:
    return {"intent": "edit", "record": ["effort", rid], "fields": {"notes": mark}}


def same(world, name: str) -> None:
    assert world.snapshot(name) == world.expected(name), diff(world.snapshot(name), world.expected(name))


def held_bytes(world, name: str) -> bytes:
    """Every byte a v1 replica keeps on disk, and its export."""
    store: Store = world.state(name).store
    files = [Path(f"{store.path}{suffix}") for suffix in ("", "-wal", "-shm")]
    return b"".join(f.read_bytes() for f in files if f.exists()) + "".join(store.export()).encode()


def gone(world, name: str, *marks: str) -> None:
    if world.driver.name == "v1":
        found = held_bytes(world, name)
        assert not [m for m in marks if m.encode() in found], name


# -- S19. device replacement and restore --------------------------------------------------------------


def test_s19_a_restored_install_is_the_store_it_came_from(world):
    world.replica("laptop")
    world.author("laptop", effort("e1"))
    world.author("laptop", edit("e1", 8))
    world.author("laptop", effort("e2", 3))
    world.author("laptop", {"intent": "remove", "record": ["effort", "e2"]})
    backup = world.export("laptop")
    world.restore("new_laptop", backup)
    assert world.snapshot("new_laptop") == world.snapshot("laptop") == world.expected("new_laptop")
    assert world.log_digest("new_laptop") == world.log_digest("laptop")
    world.rebuild("new_laptop")
    same(world, "new_laptop")


def test_s19_a_replaced_phone_gets_everything_the_laptop_had(world):
    world.replica("phone")
    world.replica("laptop")
    world.author("phone", effort("e1"))
    world.sync("phone", "laptop")
    world.author("phone", effort("e2", 3))  # pending when the phone is lost
    world.restore("new_phone", world.export("laptop"))
    same(world, "new_phone")
    assert reps(world, "new_phone") == 5 and "effort/e2" not in world.snapshot("new_phone")["records"]
    world.author("new_phone", edit("e1", 7))
    world.sync("new_phone", "laptop")
    assert reps(world, "laptop") == 7
    same(world, "laptop")


def test_s19_a_week_old_backup_catches_up_from_the_phone(world):
    world.replica("laptop")
    world.replica("phone")
    world.author("laptop", effort("e1"))
    world.sync("laptop", "phone")
    backup = world.export("laptop")
    for day in range(7):
        world.tick(86400)
        world.author("phone", effort(f"p{day}", day + 1))
        world.author("laptop", edit("e1", 10 + day))
        both_ways(world, "laptop", "phone")
    world.restore("restored", backup)
    world.sync("phone", "restored")
    assert world.snapshot("restored") == world.snapshot("phone") == world.expected("restored")


def test_s19_restoring_needs_an_empty_store(tmp_path):
    with Store(tmp_path / "a.db") as a, Store(tmp_path / "b.db") as b:
        a.write({"kind": "human", "id": "me"}, [{"kind": "setting", "id": "home_zone", "op": "put",
                                                  "payload": {"value": "UTC"}}])
        b.write({"kind": "human", "id": "me"}, [{"kind": "setting", "id": "home_zone", "op": "put",
                                                  "payload": {"value": "UTC"}}])
        with pytest.raises(Exception, match="empty"):
            b.restore(a.export())


def test_s19_a_cut_short_export_restores_nothing(tmp_path):
    with Store(tmp_path / "a.db") as a, Store(tmp_path / "b.db") as b:
        for n in range(3):
            a.write({"kind": "human", "id": "me"}, [{"kind": "location", "id": f"place_{n}", "op": "put",
                                                      "payload": {"name": f"Place {n}"}}])
        lines = list(a.export())
        with pytest.raises(Exception, match="cut short"):
            b.restore(lines[:-1])
        assert b.position() == 0 and b.store_id != a.store_id


# -- S20. erasure ---------------------------------------------------------------------------------------


def test_s20_an_erased_record_with_edits_leaves_nothing_anywhere(world):
    world.replica("phone")
    world.replica("laptop")
    world.replica("old")
    world.author("phone", noted("e1", MARK(1)))
    world.author("phone", renote("e1", MARK(2)))
    both_ways(world, "phone", "laptop", "old")
    if world.driver.name == "v1":
        assert MARK(1).encode() in held_bytes(world, "laptop")  # the search would find it
    world.author("phone", {"intent": "erase", "record": ["effort", "e1"]})
    world.sync("phone", "laptop")
    for name in ("phone", "laptop"):
        same(world, name)
        assert world.snapshot(name)["erased"] == ["effort/e1"]
        gone(world, name, MARK(1), MARK(2))
        world.rebuild(name)
        same(world, name)
    world.sync("laptop", "old")  # synced before the erase, and gets it later
    same(world, "old")
    gone(world, "old", MARK(1), MARK(2))


def test_s20_an_edit_arriving_after_the_erase_is_stored_blank(world):
    world.replica("phone")
    world.replica("laptop")
    world.author("phone", noted("e1", MARK(1)))
    world.sync("phone", "laptop")
    world.author("laptop", renote("e1", MARK(3)))
    world.author("phone", {"intent": "erase", "record": ["effort", "e1"]})
    both_ways(world, "phone", "laptop")
    for name in ("phone", "laptop"):
        same(world, name)
        gone(world, name, MARK(1), MARK(3))
    assert world.log_digest("phone") == world.log_digest("laptop")


def test_s20_erasing_a_record_in_conflict_ends_the_conflict(world):
    world.replica("phone")
    world.replica("laptop")
    world.author("phone", noted("e1", MARK(1)))
    world.sync("phone", "laptop")
    world.author("phone", renote("e1", MARK(4)))
    world.author("laptop", renote("e1", MARK(5)))
    both_ways(world, "phone", "laptop")
    assert "effort/e1" in world.snapshot("phone")["conflicts"]
    world.author("laptop", {"intent": "erase", "record": ["effort", "e1"]})
    both_ways(world, "phone", "laptop")
    for name in ("phone", "laptop"):
        same(world, name)
        assert not world.snapshot(name)["conflicts"]
        gone(world, name, MARK(1), MARK(4), MARK(5))


def test_s20_an_export_restored_after_an_erase_holds_nothing_of_it(world):
    world.replica("laptop")
    world.author("laptop", noted("e1", MARK(6)))
    world.author("laptop", {"intent": "erase", "record": ["effort", "e1"]})
    world.restore("restored", world.export("laptop"))
    same(world, "restored")
    gone(world, "restored", MARK(6))


@pytest.mark.parametrize("driver", ["v1"])
def test_s20_an_erased_source_record_and_its_correction_leave_nothing(world, driver):
    world.replica("laptop")
    world.replica("phone")
    wid = "9a1b0000-0000-4000-8000-0000000000e1"
    workout = {**whoop_workout(wid, "2026-03-02T10:00:00.000Z", "2026-03-02T10:50:00.000Z",
                               "2026-03-02T11:00:00.000Z"), "sport_name": MARK(7)}
    world.ingest("laptop", "whoop", {"workout": [workout]})
    world.author("laptop", {"intent": "correct_import", "source": "whoop", "source_id": wid,
                            "fields": {"distance_m": 4321.5}})
    world.sync("laptop", "phone")
    assert MARK(7).encode() in held_bytes(world, "phone") and b"4321.5" in held_bytes(world, "phone")
    world.driver.author(world.state("laptop"), {"intent": "erase", "source": "whoop", "source_id": wid},
                        "2026-03-02T12:00:00.000Z",
                        {"kind": "human", "id": "me"})
    store = world.state("laptop").store
    world.state("phone").store.accept_many(store.events())
    for name in ("laptop", "phone"):
        assert world.occurrences(name) == []
        assert world.source_history(name, "whoop", wid) == []
        gone(world, name, MARK(7), "4321.5")
