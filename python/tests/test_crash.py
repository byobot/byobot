"""Kill byobot at every write step of the work that changes a store, then check what the next start finds.

A child process does the work and kills itself just before its Nth write: an SQLite statement that changes the
store, or a file the cloud folder creates or deletes. Then the store must open, hold every event it held before and
nothing that wasn't written, read the same as a rebuild from its logbook, and finish the work when run again, ending
where an uninterrupted run ends.

Fast runs kill at a spread of steps; BYOBOT_SOAK=1 kills at every one.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
SOAK = bool(os.environ.get("BYOBOT_SOAK"))
KILLED = 77
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "COMMIT", "RELEASE", "PRAGMA USER")
PHONE = "0193a0b0-0000-7000-8000-00000000ab01"
ME = {"kind": "human", "id": "me"}
SESSION = "0193a0b0-0000-7000-a000-000000000100"


# -- the child -------------------------------------------------------------------

def arm(kill_at: int) -> list[int]:
    """Count write steps from here on, and die just before step `kill_at` (never, if it is 0)."""
    from cloud_doubles import LocalFolder

    count = [0]

    def step():
        count[0] += 1
        if count[0] == kill_at:
            if hasattr(signal, "SIGKILL"):
                os.kill(os.getpid(), signal.SIGKILL)
            os._exit(KILLED)

    connect = sqlite3.connect

    def counted(*args, **kw):
        conn = connect(*args, **kw)
        conn.set_trace_callback(lambda sql: step() if sql.lstrip().upper().startswith(WRITES) else None)
        return conn

    sqlite3.connect = counted
    _repeatable()
    for name in ("create", "delete"):
        real = getattr(LocalFolder, name)

        def wrapped(self, *args, _real=real):
            step()
            _real(self, *args)
        setattr(LocalFolder, name, wrapped)
    return count


def _repeatable() -> None:
    """Make a run write the ids and clock readings an uninterrupted run would: a new id names the store's arrival
    position it's written at, and the clock stands still."""
    import uuid
    from datetime import datetime, timezone

    from byobot.core.store import Store

    opened, last = [], {"at": None, "n": 0}
    init = Store.__init__

    def tracked(self, *args, **kw):
        init(self, *args, **kw)
        opened.append(self)

    def new_id() -> str:
        at = opened[-1].position() if opened else 0
        last["n"] = last["n"] + 1 if last["at"] == at else 0
        last["at"] = at
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"byobot-crash:{at}:{last['n']}"))

    Store.__init__ = tracked
    init.__kwdefaults__.update(new_id=new_id, wall=lambda: datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc))


def workouts(n: int, revision: int = 0) -> list[tuple[str, str, dict]]:
    return [("workout", f"w{i}", {
        "id": f"w{i}", "start": f"2026-03-{1 + i % 28:02d}T15:00:00.000Z", "end": f"2026-03-{1 + i % 28:02d}T16:00:00.000Z",
        "timezone_offset": "-05:00", "sport_name": "running", "updated_at": f"2026-03-29T1{revision}:00:00Z",
        "score_state": "SCORED", "score": {"average_heart_rate": 130 + i + revision, "distance_meter": 5000 + i}})
        for i in range(n)]


def phone_events(n: int, offset: int = 0) -> list[dict]:
    return [{"id": f"0193a0b0-0000-7000-8000-0000000{offset + i:05d}", "schema_version": 1, "op": "put",
             "record": {"kind": "effort", "id": f"0193a0b0-0000-7000-9000-0000000{offset + i:05d}"},
             "hlc": f"{1795400000000 + 1000 * (offset + i)}-000000", "ts": "2026-11-23T08:00:00-05:00",
             "device_id": PHONE, "actor": ME, "prev": [], "kind_version": 1,
             "payload": {"activity_id": "barbell_back_squat",
                         "time": {"start": "2026-11-23T07:40:00-05:00"}, "metrics": {"reps": {"value": 5}}}}
            for i in range(n)]


def work(op: str, root: Path) -> None:
    """What the child does to the store in `root`."""
    from cloud_doubles import LocalFolder
    from byobot.cloud.keeper import Keeper
    from byobot.core import commands, relay, sources
    from byobot.core.store import Store

    db = root / "byobot.db"
    if op == "reshape":  # an upgrade whose derived tables have another shape
        Store(db).close()
        return
    with Store(db) as store:
        if op == "import":
            sources.ingest(store, "whoop", workouts(30))
            sources.ingest(store, "whoop", workouts(30, revision=1), deleted=[("workout", "w3")])
        elif op == "rebuild":
            store.rebuild()
        elif op == "erase":
            if not store.record("session", SESSION)["erased"]:  # done before the kill: the retry finds it so
                commands.erase(store, ME, "session", SESSION, with_efforts=True)
        elif op == "pair":
            store.pair(PHONE, "Example iPhone", "0" * 64)
            relay.push_events(store, PHONE, {"events": phone_events(20, offset=100)})
        elif op == "restore":
            store.restore((root / "export.jsonl").read_text().splitlines())
        elif op == "drive":
            keeper = Keeper(store, LocalFolder(root / "cloud", store.store_id))
            keeper.sync()
            keeper.sync()
        else:
            raise SystemExit(f"no op {op}")


# -- the parent ------------------------------------------------------------------

def seed(root: Path, op: str) -> None:
    """The store each run starts from."""
    from cloud_doubles import LocalFolder
    from byobot.cloud.keeper import Keeper
    from cloud_doubles import Relay
    from byobot.core import commands, relay, sources
    from byobot.core.store import Store

    db = root / "byobot.db"
    with Store(db) as store:
        sources.ingest(store, "whoop", workouts(5))
        commands.log(store, ME, [
            {"kind": "session", "id": SESSION, "payload": {
                "name": "Example session", "time": {"start": "2026-03-02T07:30:00-05:00"}}},
            *({"kind": "effort", "id": f"0193a0b0-0000-7000-a000-00000000000{i}", "payload": {
                "activity_id": "barbell_back_squat",
                "time": {"start": f"2026-03-02T07:4{i}:00-05:00"}, "notes": "example-secret-note",
                "links": {"part_of": {"kind": "session", "id": SESSION}},
                "metrics": {"reps": {"value": 5}, "load": {"value": 100 + i, "unit": "kg"}}}} for i in range(4))])
        relay.push_events(store, PHONE, {"events": phone_events(5)})
        if op == "reshape":
            store.conn.execute("UPDATE meta SET value = 'older' WHERE key = 'shape'")
        if op == "restore":
            (root / "export.jsonl").write_text("\n".join(store.export()))
        if op == "drive":
            folder = LocalFolder(root / "cloud", store.store_id)
            keeper = Keeper.setup(store, folder)
            phone = Relay.join(folder, keeper.join_code()["link"], PHONE)
            keeper.sync()
            phone.sync()
            phone.log(phone_events(15, offset=200))
            phone.sync()
    if op == "restore":
        db.unlink()
        for extra in root.glob("byobot.db-*"):
            extra.unlink()


def child(op: str, root: Path, kill_at: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE.parent.parent / "src"),
                                                        os.environ.get("PYTHONPATH", "")])}
    return subprocess.run([sys.executable, str(HERE), op, str(root), str(kill_at)], env=env, capture_output=True,
                          text=True, timeout=120)


def derived(store) -> dict:
    """Every table a store derives from its logbook, row by row."""
    from byobot.core import occurrences, reducers

    tables = sorted({t for ts in reducers.TABLES.values() for t in ts} | {"metrics", *occurrences.TABLES,
                                                                         "records", "unprojected"})
    return {t: sorted(json.dumps([*row], default=str) for row in store.conn.execute(f"SELECT * FROM {t}"))
            for t in tables}


def held(root: Path) -> tuple[set, dict]:
    from byobot.core.store import Store

    copy = root.parent / f"{root.name}-check"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(root, copy)
    with Store(copy / "byobot.db") as store:
        events = {e["id"]: json.dumps(e, sort_keys=True) for e in store.events()}
        before = derived(store)
        store.rebuild()
        assert derived(store) == before, "the derived tables don't read as the logbook says"
    shutil.rmtree(copy)
    return set(events), before


def fresh(template: Path, root: Path) -> Path:
    shutil.copytree(template, root)
    return root


def steps(op: str, template: Path, tmp_path: Path) -> tuple[int, set, dict, set]:
    """How many write steps an uninterrupted run takes, and what it starts and ends with."""
    root = fresh(template, tmp_path / "clean")
    start = held(root)[0] if (root / "byobot.db").exists() else set()
    done = child(op, root, 0)
    assert done.returncode == 0, done.stderr
    end, tables = held(root)
    return int(done.stdout.split()[-1]), start, tables, end


def no_secret_left(root: Path) -> None:
    raw = b"".join(p.read_bytes() for p in root.glob("byobot.db*"))
    assert b"example-secret-note" not in raw, "an erased note is still in the store's files"


OPS = ("import", "rebuild", "reshape", "erase", "pair", "restore", "drive")


@pytest.mark.parametrize("op", OPS)
def test_a_kill_at_any_write_step_loses_nothing_and_finishes_on_the_next_run(tmp_path, op):
    template = tmp_path / "template"
    template.mkdir()
    seed(template, op)
    total, start, tables, end = steps(op, template, tmp_path)
    assert total > 0
    picks = range(1, total + 1) if SOAK else sorted({1, total, *range(1, total + 1, max(1, total // 12))})
    for n in picks:
        root = fresh(template, tmp_path / f"kill-{n}")
        run = child(op, root, n)
        assert run.returncode in (KILLED, -9), f"{op}: no kill at step {n} of {total}: {run.stderr}"
        if (root / "byobot.db").exists():
            events, _ = held(root)
            assert start <= events <= end, f"{op}: killed at step {n} of {total}, the logbook lost or gained events"
        again = child(op, root, 0)
        if op == "drive":
            again = child(op, root, 0)  # the folder settles over a couple of syncs
        assert again.returncode == 0, f"{op}: killed at step {n} of {total}, the next run failed: {again.stderr}"
        events, after = held(root)
        assert events == end, f"{op}: killed at step {n} of {total}, the next run ended with other events"
        assert after == tables, f"{op}: killed at step {n} of {total}, the next run reads differently"
        if op == "erase":
            no_secret_left(root)
        shutil.rmtree(root)


if __name__ == "__main__":
    _, op, where, kill_at = sys.argv
    count = arm(int(kill_at))
    work(op, Path(where))
    print(count[0])
