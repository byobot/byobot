"""Drive deep review, proving a round-1 suspicion: a relay rejects a whole plan segment that carries a message it
doesn't know. Once a newer Mac adds any message to the plan, an older phone takes no plan and no receipts again: it
never learns its sets landed, keeps every segment in `sent`, and sends its samples again each epoch."""

from _drive_helpers import Clock, effort, joined
from cloud_doubles import MemoryFolder
from byobot.cloud.keeper import Keeper


def test_an_older_phone_still_takes_a_plan_that_carries_a_newer_message(store, monkeypatch):
    folder = MemoryFolder()
    keeper = Keeper.setup(store, folder, Clock())
    relay = joined(keeper, folder)
    real = Keeper._segment

    def newer_keeper(self, kind, seq, messages, epoch=None):
        if kind == "plan":  # a later byobot adds a message to the plan segment
            messages = [*messages, {"reminders": {"items": []}}]
        return real(self, kind, seq, messages, epoch)

    monkeypatch.setattr(Keeper, "_segment", newer_keeper)
    sent = effort(relay.device_id, 1)
    relay.log([sent])
    relay.sync()
    keeper.sync()
    assert store.event(sent["id"])
    relay.sync()
    assert relay.events == {} and relay.sent == {}, "the phone never learns the Mac read its set"
