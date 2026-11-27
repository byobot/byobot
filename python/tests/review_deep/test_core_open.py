"""Deep review (core): opening a store, and the version and identity it keeps."""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _core_helpers import Wall, open_store, whoop_workout  # noqa: E402

from byobot.core import reducers, sources  # noqa: E402
from byobot.core.store import Store  # noqa: E402


def _race(path: Path, monkeypatch, first_writes: bool) -> dict:
    """Two processes open a store that doesn't exist yet. Both read user_version 0 before either commits; the
    first finishes creating it (and, if asked, imports a WHOOP workout) while the second waits for its write lock."""
    first_in_tx, release, second_read, first_done = (threading.Event() for _ in range(4))
    seed, real_tx = reducers.seed_library, Store._tx

    def slow_seed(conn):
        if threading.current_thread().name == "first":
            first_in_tx.set()
            assert release.wait(10)
        seed(conn)

    def tx(self):
        if threading.current_thread().name == "second" and not second_read.is_set():
            second_read.set()  # user_version (0) is read; its transaction starts once the first is done
            assert first_done.wait(10)
        return real_tx(self)

    monkeypatch.setattr(reducers, "seed_library", slow_seed)
    monkeypatch.setattr(Store, "_tx", tx)
    seen = {}

    def first():
        with Store(path, wall=Wall()) as s:
            seen["first"] = (s.store_id, s._get("store_key"))
            if first_writes:
                sources.ingest(s, "whoop", [("workout", "1001", whoop_workout(
                    "1001", "2026-03-02T14:00:00.000Z", "2026-03-02T15:00:00.000Z"))])
        first_done.set()

    def second():
        with Store(path, wall=Wall()) as s:
            seen["second"] = (s.store_id, s._get("store_key"))

    a = threading.Thread(target=first, name="first")
    a.start()
    assert first_in_tx.wait(10)
    b = threading.Thread(target=second, name="second")
    b.start()
    assert second_read.wait(10)
    release.set()
    a.join(20)
    b.join(20)
    monkeypatch.undo()
    return seen


def test_two_processes_creating_one_store_keep_the_first_ones_identity(tmp_path, monkeypatch):
    seen = _race(tmp_path / "byobot.db", monkeypatch, first_writes=False)
    assert seen["first"] == seen["second"], "the second open replaced the store id and key the first handed out"


def test_a_store_created_by_two_processes_at_once_can_still_be_restored(tmp_path, monkeypatch):
    """The import made under the first key no longer verifies under the second, so the backup is refused whole."""
    path = tmp_path / "byobot.db"
    _race(path, monkeypatch, first_writes=True)
    with open_store(path) as s:
        backup = list(s.export())
    with open_store(tmp_path / "restored.db", device_id="new-laptop") as fresh:
        fresh.restore(backup)
        assert len(fresh.events()) == 1
