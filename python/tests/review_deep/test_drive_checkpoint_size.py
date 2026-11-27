"""Drive deep review, proving a round-1 suspicion and widening it: writers cut checkpoint parts (and segments) by
count, 2000 events, never by size, while readers refuse a body past MAX_BODY (32 MiB). `cloud recover` then skips the
part it can't read without a word, reports success, and starts a new epoch whose checkpoint lacks those events; once
phones move, cleanup deletes the old part and the events are gone from Drive for good.

Real parts need about 16 KB an event to pass 32 MiB (Apple Health samples with large `raw`, sent by `byobot ingest`).
The test scales MAX_BODY down to keep it fast; the writer and the reader use the same constant, so the asymmetry is
the same."""

from _drive_helpers import ME, Clock
from byobot.cloud import segment
from byobot.cloud.folder import MemoryFolder
from byobot.cloud.keeper import Keeper
from byobot.core import commands
from byobot.core.store import Store


def test_recover_gets_every_event_or_says_it_cant(store, tmp_path, monkeypatch):
    monkeypatch.setattr(segment, "MAX_BODY", 128 * 1024)
    for n in range(40):
        commands.log(store, ME, [{"kind": "location", "id": f"example-gym-{n}",
                                  "payload": {"name": f"Gym {n}", "notes": f"{n:04d}" + "x" * 3900}}])
    folder, clock = MemoryFolder(), Clock()
    keeper = Keeper.setup(store, folder, clock, recovery=True)
    fresh = Store(tmp_path / "new-mac.db")
    try:
        Keeper.recover(fresh, folder, keeper.recovery_code, clock)
    except Exception:
        return  # saying it can't is fine
    assert len(fresh.records("location")) == 40, "recover reported success with part of the logbook"
