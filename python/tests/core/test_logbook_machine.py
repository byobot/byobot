"""Randomized histories on the byobot logbook, checked against the reference model at every step.

Three replicas author every intent the core has, import source revisions and deletions, write as a newer
install, upgrade, and sync random subsets in shuffled order with duplicates. After each step every replica
matches the model; a rebuild, a shuffled replay of its log into a fresh store and an erase are checked
as they happen; at the end everything is delivered everywhere and all replicas agree.
"""

from __future__ import annotations

import random
import tempfile
from datetime import timedelta

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from .driver import Unsupported
from .drivers.v1 import V1
from .harness import AGENT, HUMAN, World, day
from .scenarios import health_workout, whoop_workout
from .snapshot import diff, equal
from .test_durability import held_bytes

REPLICAS = ("laptop", "phone", "tablet")
ACTIVITIES = ("x.row", "x.squat")
EFFORTS = ("e1", "e2", "e3")
SESSIONS = ("s1", "s2")
WORKOUTS = ("9a1b0000-0000-4000-8000-0000000000a1", "9a1b0000-0000-4000-8000-0000000000a2")
EDITABLE = [("effort", i) for i in EFFORTS] + [("session", i) for i in SESSIONS] + [
    ("activity", "x.row"), ("location", "example_gym"), ("plan", "p1"), ("planned_session", "ps1")]

replica = st.integers(0, len(REPLICAS) - 1)


class Logbook(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.world = World(V1(), self.tmp.name)
        self.marks: dict[str, set[str]] = {}
        self.restores = 0

    def teardown(self):
        try:
            self._settle(random.Random(0))
            for name in REPLICAS:
                self.world.upgrade(name, 2)
            first = self.world.snapshot(REPLICAS[0])
            for name in REPLICAS:
                self._matches(name)
                assert equal(first, self.world.snapshot(name)), diff(first, self.world.snapshot(name))
                self._no_erased_bytes(name)
        finally:
            self.world.close()
            self.tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _mark(self, record: str) -> str:
        mark = f"zq-mark-{sum(map(len, self.marks.values()))}-zq"
        self.marks.setdefault(record, set()).add(mark)
        return mark

    def _author(self, r: int, intent: dict, actor: dict = HUMAN):
        self.world.tick(60)
        try:
            return self.world.author(REPLICAS[r], intent, actor)
        except Unsupported:
            return None

    def _matches(self, name: str) -> None:
        got, want = self.world.snapshot(name), self.world.expected(name)
        assert equal(got, want), diff(got, want)
        assert self.world.occurrences(name) == self.world.expected_occurrences(name), name

    def _no_erased_bytes(self, name: str) -> None:
        gone = self.world.shadows[name].gone
        kept = {m for record, ms in self.marks.items() if record not in gone for m in ms}
        marks = [m for record in gone for m in self.marks.get(record, ()) if m not in kept]
        if marks:
            found = held_bytes(self.world, name)
            assert [m for m in marks if m.encode() in found] == [], name

    def _erasable(self, r: int, record: str) -> bool:
        """The command layer erases only a record it holds that isn't erased yet."""
        shadow = self.world.shadows[REPLICAS[r]]
        return bool(shadow.by_record.get(record)) and record not in shadow.gone

    def _settle(self, rng: random.Random) -> None:
        for name in REPLICAS:
            held = self.world.replicas[name].held
            todo = [c for c in self.world.ordered(set(self.world.changes)) if c.id not in held]
            rng.shuffle(todo)
            outcomes = self.world.deliver(name, todo)
            assert [o for o in outcomes if o[0] == "rejected"] == [], outcomes

    # -- setup --------------------------------------------------------------

    @initialize()
    def start(self):
        for name in REPLICAS:
            self.world.replica(name)
        for a in ACTIVITIES:
            self.world.author("laptop", {"intent": "define_activity", "record": {"id": a, "name": a}})
        for name in REPLICAS[1:]:
            self.world.sync("laptop", name)

    # -- authoring ----------------------------------------------------------

    @rule(r=replica, rid=st.sampled_from(EFFORTS), reps=st.integers(1, 9), activity=st.sampled_from(ACTIVITIES),
          session=st.none() | st.sampled_from(SESSIONS), at=st.integers(0, 6))
    def log_effort(self, r, rid, reps, activity, session, at):
        record = {"id": rid, "activity_id": activity, "performed_at": day(at), "metrics": {"reps": {"value": reps}},
                  "notes": self._mark(f"effort/{rid}")}
        if session:
            record["session_id"] = session
        self._author(r, {"intent": "log_effort", "record": record})

    @rule(r=replica, sid=st.sampled_from(SESSIONS), at=st.integers(0, 6), inline=st.booleans(),
          planned=st.booleans())
    def log_session(self, r, sid, at, inline, planned):
        minute = SESSIONS.index(sid) * 5
        record = {"id": sid, "started_at": day(at, 10, minute), "ended_at": day(at, 11, minute), "notes": self._mark(f"session/{sid}")}
        if planned:
            record["planned_session_id"] = "ps1"
        if inline:
            rid = f"{sid}x"
            record["efforts"] = [{"id": rid, "activity_id": "x.row", "notes": self._mark(f"effort/{rid}")}]
        self._author(r, {"intent": "log_session", "record": record})

    @rule(r=replica, name=st.sampled_from(("Home", "Gym")))
    def define_location(self, r, name):
        self._author(r, {"intent": "define_location", "record": {"id": "example_gym", "name": name}})

    @rule(r=replica)
    def define_plan(self, r):
        self._author(r, {"intent": "define_plan", "record": {"id": "p1", "name": "Strength", "starts_on": "2026-03-01",
                                                              "notes": self._mark("plan/p1")}}, AGENT)

    @rule(r=replica, swap=st.booleans(), rounds=st.integers(1, 4), again=st.booleans())
    def plan_session(self, r, swap, rounds, again):
        blocks = [{"rounds": rounds, "efforts": [{"activity_id": "x.squat" if swap else "x.row"}]},
                  {"rounds": 3, "efforts": [{"activity_id": "x.row"}]}]
        record = {"id": "ps1", "scheduled_on": "2026-03-04", "name": "Legs", "plan_id": "p1"}
        if again:
            self._author(r, {"intent": "replan_session", "record": {**record, "blocks": blocks}}, AGENT)
        else:
            ids = [{"id": f"ps1.b{i}", **b, "efforts": [{"id": f"ps1.pe{i}", **b["efforts"][0]}]}
                   for i, b in enumerate(blocks)]
            self._author(r, {"intent": "plan_session", "record": {**record, "blocks": ids}}, AGENT)

    @rule(r=replica, which=st.sampled_from(EDITABLE), reps=st.integers(1, 9), renote=st.booleans())
    def edit(self, r, which, reps, renote):
        kind, rid = which
        record = f"{kind}/{rid}"
        if kind == "effort":
            fields = {"metrics": {"reps": {"value": reps}}}
        elif kind in ("activity", "location", "planned_session"):
            fields = {"name": f"name {reps}"}
        else:
            fields = {}
        if renote or not fields:
            fields["notes"] = self._mark(record)
        self._author(r, {"intent": "edit", "record": [kind, rid], "fields": fields})

    @rule(r=replica, which=st.sampled_from(EDITABLE[:5]))
    def remove(self, r, which):
        self._author(r, {"intent": "remove", "record": list(which)})

    @rule(r=replica, pick=st.integers(0, 50))
    def undo(self, r, pick):
        name = REPLICAS[r]
        held = [c for c in self.world.ordered(self.world.replicas[name].held)
                if c.events and c.writes and len(c.writes) == len(c.events)
                and all(w.kind not in ("source_record",) and w.op in ("put", "retract") for w in c.writes)
                and all(e["record"]["kind"] != "meal" for e in c.events)]
        if held:
            self._author(r, {"intent": "undo", "change": held[-1 - pick % min(len(held), 6)].id})

    @rule(r=replica, which=st.sampled_from(EDITABLE[:5]), choice=st.integers(0, 3))
    def resolve(self, r, which, choice):
        record = f"{which[0]}/{which[1]}"
        versions = self.world.expected(REPLICAS[r])["conflicts"].get(record, [])
        if versions:
            pick = "remove" if choice == 3 else choice % len(versions)
            self._author(r, {"intent": "resolve", "record": list(which), "choice": pick})

    @rule(r=replica, which=st.sampled_from(EDITABLE[:5]))
    def erase(self, r, which):
        if not self._erasable(r, f"{which[0]}/{which[1]}"):
            return
        self._author(r, {"intent": "erase", "record": list(which)})
        self._no_erased_bytes(REPLICAS[r])

    # -- sources ------------------------------------------------------------

    @rule(r=replica, wid=st.sampled_from(WORKOUTS), revision=st.integers(0, 3), at=st.integers(0, 6))
    def import_whoop(self, r, wid, revision, at):
        minute = WORKOUTS.index(wid) * 2
        start, end = day(at, 10, 2 + minute), day(at, 10, 58 + minute)
        workout = whoop_workout(wid, start, end, day(at, 12, revision), strain=10.0 + revision)
        self.world.tick(60)
        self.world.ingest(REPLICAS[r], "whoop", {"workout": [workout]})

    @rule(r=replica, hid=st.sampled_from(("h1", "h2")), distance=st.integers(1, 9), at=st.integers(0, 6),
          delete=st.booleans(), stamped=st.booleans())
    def import_health(self, r, hid, distance, at, delete, stamped):
        self.world.tick(60)
        if delete:
            batch = {"samples": [], "deleted": [("HKWorkoutTypeIdentifier", hid)]}
        else:
            minute = int(hid[1]) * 2
            sample = health_workout(hid, day(at, 10, minute - 1), day(at, 10, 57 + minute), distance * 1000.0)
            if stamped:  # a changed sample says when; one that doesn't is ordered by its id
                sample["raw"]["modified_at"] = day(at, 12, distance)
            batch = [sample]
        self.world.ingest(REPLICAS[r], "apple-health", batch)

    @rule(r=replica, wid=st.sampled_from(WORKOUTS), distance=st.none() | st.integers(1, 20))
    def correct_import(self, r, wid, distance):
        self._author(r, {"intent": "correct_import", "source": "whoop", "source_id": wid,
                         "fields": {"distance_m": None if distance is None else distance * 1000}})

    @rule(r=replica, wid=st.sampled_from(WORKOUTS), sid=st.sampled_from(SESSIONS))
    def same_as(self, r, wid, sid):
        self._author(r, {"intent": "same_as", "source": "whoop", "source_id": wid, "session": sid})

    @rule(r=replica, wid=st.sampled_from(WORKOUTS))
    def erase_source(self, r, wid):
        if not self._erasable(r, f"source_record/whoop:workout:{wid}"):
            return
        self._author(r, {"intent": "erase", "source": "whoop", "source_id": wid,
                         "record": ["source_record", f"whoop:workout:{wid}"]})

    # -- versions -----------------------------------------------------------

    @rule(r=replica, variant=st.sampled_from(("unknown_kind", "newer_version", "extra_field")), pick=st.integers(0, 50))
    def write_as_newer_install(self, r, variant, pick):
        name = REPLICAS[r]
        logs = [c for c in self.world.ordered(self.world.replicas[name].held)
                if c.events and len(c.events) == 1 and c.writes and c.writes[0].kind == "effort"
                and c.writes[0].op == "put" and "~" not in c.writes[0].id and c.events[0]["record"]["kind"] == "effort"]
        made = {c.writes[0].id for c in self.world.changes.values() if c.writes and "~" in c.writes[0].id}
        logs = [c for c in logs if not any(m.startswith(c.writes[0].id + "~") for m in made)]
        if logs:
            change = self.world.newer(logs[pick % len(logs)].id, variant)
            if variant == "unknown_kind":  # the copy is another record holding the same notes
                rid = change.writes[0].rid
                self.marks.setdefault(f"meal/{rid}", set()).update(self.marks.get(f"effort/{rid}", ()))
            self.world.replicas[name].held.discard(change.id)
            outcomes = self.world.deliver(name, [change])
            assert [o for o in outcomes if o[0] == "rejected"] == [], outcomes

    @rule(r=replica)
    def upgrade(self, r):
        self.world.upgrade(REPLICAS[r], 2)

    # -- delivery -----------------------------------------------------------

    @rule(src=replica, dst=replica, seed=st.integers(0, 2**16), share=st.integers(1, 4), dupes=st.integers(0, 3))
    def sync(self, src, dst, seed, share, dupes):
        if src == dst:
            return
        rng = random.Random(seed)
        missing = self.world.missing(REPLICAS[src], REPLICAS[dst])
        chosen = rng.sample(missing, len(missing) * share // 4) if share < 4 else missing
        held = self.world.ordered(self.world.replicas[REPLICAS[dst]].held)
        again = [rng.choice(held) for _ in range(dupes)] if held else []
        batch = chosen + again
        rng.shuffle(batch)
        outcomes = self.world.deliver(REPLICAS[dst], batch)
        assert [o for o in outcomes if o[0] == "rejected"] == [], outcomes
        self._no_erased_bytes(REPLICAS[dst])

    @rule(r=replica, hours=st.integers(-72, 72))
    def skew(self, r, hours):
        self.world.clock(REPLICAS[r]).skew = timedelta(hours=hours)

    # -- the logbook's own checks --------------------------------------------

    @rule(r=replica)
    def rebuild_equals_incremental(self, r):
        name = REPLICAS[r]
        before, occurrences = self.world.snapshot(name), self.world.occurrences(name)
        self.world.rebuild(name)
        assert equal(before, self.world.snapshot(name)), diff(before, self.world.snapshot(name))
        assert occurrences == self.world.occurrences(name)

    @rule(r=replica, seed=st.integers(0, 2**16))
    def replay_in_any_order_gives_the_same_state(self, r, seed):
        name, driver = REPLICAS[r], self.world.driver
        events = driver.log_events(self.world.state(name))
        rng = random.Random(seed)
        shuffled = events + [rng.choice(events) for _ in range(min(3, len(events)))]
        rng.shuffle(shuffled)
        self.restores += 1
        fresh = driver.open(f"replay{self.restores}", self.world.directory)
        try:
            if self.world.shadows[name].version > 1:
                driver.upgrade(fresh, self.world.shadows[name].version)
            outcomes = driver.deliver(fresh, shuffled)
            assert [o for o in outcomes if o[0] == "rejected"] == [], outcomes
            want, got = self.world.snapshot(name), driver.snapshot(fresh)
            assert equal(want, got), diff(want, got)
            assert self.world.occurrences(name) == driver.occurrences(fresh)
        finally:
            driver.close(fresh)

    @invariant()
    def every_replica_matches_the_model(self):
        if self.world.replicas:
            for name in REPLICAS:
                self._matches(name)


Logbook.TestCase.settings = settings(settings.default, stateful_step_count=30)
test_logbook = Logbook.TestCase


def test_a_sample_without_a_revision_time_imported_on_two_devices_reads_the_same_on_both():
    state = Logbook()
    state.start()
    for r, at in ((0, 1), (0, 0), (1, 1)):
        state.world.tick(60)
        sample = health_workout("h1", day(at, 10, 1), day(at, 10, 59), 1000.0)
        state.world.ingest(REPLICAS[r], "apple-health", [sample])
    state.teardown()


def test_first_imports_without_a_revision_time_on_three_devices_read_the_same_on_all():
    state = Logbook()
    state.start()
    for r, at in ((0, 1), (1, 0), (2, 1)):
        state.world.tick(60)
        sample = health_workout("h1", day(at, 10, 1), day(at, 10, 59), 1000.0)
        state.world.ingest(REPLICAS[r], "apple-health", [sample])
    state.teardown()
