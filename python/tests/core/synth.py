"""A seeded synthetic persona: years of training, wearables, a scale and travel. Same seed, same bytes.

Every value comes from a plausible distribution, never from anyone's data. Devices are
`laptop`, `phone` and its replacement `phone2`; the only location is `example-gym`.

`persona(seed, days)` returns the steps in time order and the ground truth the oracles need:
every real workout (for single counting), every authored record's intended local date, and
the source records that were revised.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .harness import AGENT, HUMAN, World
from .snapshot import key

EPOCH = date(2026, 1, 5)  # a Monday
HOME_ZONE = (timedelta(hours=-5), timedelta(hours=-4))  # standard and daylight offsets
TRIP_OFFSETS = (timedelta(hours=0), timedelta(hours=1), timedelta(hours=9))
LIFTS = (
    ("barbell_back_squat", "Back squat", 100), ("bench_press", "Bench press", 80),
    ("deadlift", "Deadlift", 140), ("overhead_press", "Overhead press", 50), ("barbell_row", "Barbell row", 70),
    ("pull_up", "Pull-up", 0), ("romanian_deadlift", "Romanian deadlift", 90), ("lunge", "Lunge", 30),
    ("dip", "Dip", 0), ("plank", "Plank", 0),
)
SWAPS = {"barbell_back_squat": "lunge", "bench_press": "dip", "barbell_row": "pull_up"}
OTHER = (("running", 0.55), ("cycling", 0.3), ("yoga", 0.15))
HEALTHKIT = {"weightlifting_msk": "traditionalStrengthTraining", "running": "running", "cycling": "cycling",
             "yoga": "yoga"}


@dataclass(frozen=True)
class Step:
    at: datetime  # true time
    op: str  # author | undo | ingest | sync | clock | device
    replica: str
    args: tuple = ()


@dataclass
class Persona:
    seed: int
    days: int
    steps: list[Step] = field(default_factory=list)
    workouts: list[dict] = field(default_factory=list)  # ground truth: one entry per real workout
    local_dates: dict[str, str] = field(default_factory=dict)
    revised: list[str] = field(default_factory=list)

    def count(self, op: str) -> int:
        return sum(1 for s in self.steps if s.op == op)


def dst(day: date) -> bool:
    """Daylight time in the home zone: second Sunday of March to first Sunday of November."""
    def sunday(month: int, n: int) -> date:
        first = date(day.year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    return sunday(3, 2) <= day < sunday(11, 1)


def stamp(at: datetime, offset: timedelta) -> str:
    return at.astimezone(timezone(offset)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Gen:
    def __init__(self, seed: int, days: int):
        self.rng = random.Random(seed)
        self.p = Persona(seed, days)
        self.n = 0
        self.trips = self._trips(days)
        self.skewed = self._windows(days, 2, 3)
        self.offline = self._windows(days, max(1, days // 365), min(30, max(1, days // 4)))
        self.replaced = days // 2 if days >= 60 else None

    # -- calendar ------------------------------------------------------------

    def _windows(self, days: int, count: int, length: int) -> list[tuple[int, int]]:
        starts = sorted(self.rng.randrange(0, max(1, days - length)) for _ in range(count))
        return [(s, s + length) for s in starts]

    def _trips(self, days: int) -> list[tuple[int, int, timedelta]]:
        return [(s, e, self.rng.choice(TRIP_OFFSETS)) for s, e in self._windows(days, 3 * max(1, days // 365), 6)]

    def offset(self, d: int) -> timedelta:
        for s, e, off in self.trips:
            if s <= d < e:
                return off
        return HOME_ZONE[dst(EPOCH + timedelta(days=d))]

    def at(self, d: int, hour: float) -> datetime:
        local = datetime.combine(EPOCH + timedelta(days=d), datetime.min.time()) + timedelta(hours=hour)
        return local.replace(tzinfo=timezone(self.offset(d))).astimezone(timezone.utc)

    def phone(self, d: int) -> str:
        return "phone2" if self.replaced is not None and d >= self.replaced else "phone"

    def id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def add(self, at: datetime, op: str, replica: str, *args) -> None:
        self.p.steps.append(Step(at, op, replica, args))

    def author(self, at: datetime, replica: str, intent: dict, actor: dict = HUMAN) -> None:
        self.add(at, "author", replica, intent, actor)

    # -- streams -------------------------------------------------------------

    def run(self) -> Persona:
        start = self.at(-1, 6)
        for activity_id, name, _ in LIFTS:
            self.author(start, "laptop", {"intent": "define_activity", "record": {"id": activity_id, "name": name}})
        self.author(start, "laptop", {"intent": "define_location",
                                      "record": {"id": "example-gym", "name": "Example gym"}})
        self.author(start, "laptop", {"intent": "define_plan", "record": {
            "id": "plan1", "name": "Strength block", "starts_on": EPOCH.isoformat()}}, AGENT)
        self.add(start, "sync", "laptop", "phone")
        for d in range(self.p.days):
            self.day(d)
        self.p.steps.sort(key=lambda s: s.at)
        return self.p

    def day(self, d: int) -> None:
        phone = self.phone(d)
        if d == self.replaced:
            self.add(self.at(d, 0.4), "sync", "phone", "laptop")
            self.add(self.at(d, 0.5), "device", phone)
            self.add(self.at(d, 0.5), "sync", "laptop", phone)
        for s, e in self.skewed:
            if d in (s, e):
                hours = self.rng.choice((-3, 3, 26)) if d == s else 0
                self.add(self.at(d, 6.5), "clock", phone, timedelta(hours=hours))
        if d % 7 in (0, 1, 3, 4):
            self.strength(d, phone)
        elif self.rng.random() < 0.7:
            self.other(d, phone)
        self.night(d)
        self.weigh_in(d)
        if not any(s <= d < e for s, e in self.offline):
            self.add(self.at(d, 21), "sync", phone, "laptop")
            self.add(self.at(d, 21.5), "sync", "laptop", phone)

    def strength(self, d: int, phone: str) -> None:
        rng = self.rng
        sid, plan_id = self.id("ps"), self.id("s")
        lifts = rng.sample(LIFTS[:9], 6)
        blocks = [{"id": f"{sid}.b{i}", "rounds": rng.choice((3, 4, 5)), "efforts": [
            {"id": f"{sid}.pe{i}", "activity_id": a, "metrics": {"reps": {"min": 5, "max": 8}}}]}
            for i, (a, _, _) in enumerate(lifts)]
        planned = {"id": sid, "plan_id": "plan1", "scheduled_on": (EPOCH + timedelta(days=d)).isoformat(),
                   "name": "Strength", "location_id": "example-gym", "blocks": blocks}
        self.author(self.at(d - 1, 20), "laptop", {"intent": "plan_session", "record": planned}, AGENT)
        if rng.random() < 0.1:
            replanned = {**planned, "blocks": [{**b, "rounds": b["rounds"] + 1} for b in blocks]}
            self.author(self.at(d, 6), "laptop", {"intent": "replan_session", "record": replanned}, AGENT)
        hour = rng.choice((6.5, 7, 17.5, 18, 23.5)) if self.offset(d) == HOME_ZONE[dst(EPOCH + timedelta(days=d))] \
            else rng.choice((7, 18))
        began = self.at(d, hour)
        self.author(began, phone, {"intent": "log_session", "record": {
            "id": plan_id, "planned_session_id": sid, "started_at": stamp(began, self.offset(d)),
            "location_id": "example-gym"}})
        self.p.local_dates[f"session/{plan_id}"] = (began.astimezone(timezone(self.offset(d)))).date().isoformat()
        minute = 0
        logged = []
        for i, block in enumerate(blocks):
            activity = block["efforts"][0]["activity_id"]
            if activity in SWAPS and rng.random() < 0.05:
                activity = SWAPS[activity]
            load = next(w for a, _, w in LIFTS if a == block["efforts"][0]["activity_id"])
            for r in range(1, block["rounds"] + 1):
                minute += rng.randint(2, 4)
                if rng.random() < 0.03:
                    continue
                when = began + timedelta(minutes=minute)
                eid = self.id("e")
                metrics = {"reps": {"value": rng.randint(4, 9)}}
                if load:
                    metrics["load"] = {"value": load + 2.5 * rng.randint(-2, 4), "unit": "kg"}
                self.author(when, phone, {"intent": "log_effort", "record": {
                    "id": eid, "session_id": plan_id, "activity_id": activity, "planned_effort_id": f"{sid}.pe{i}",
                    "round": r, "performed_at": stamp(when, self.offset(d)), "metrics": metrics}})
                self.p.local_dates[f"effort/{eid}"] = when.astimezone(timezone(self.offset(d))).date().isoformat()
                if rng.random() < 0.02:
                    self.add(when + timedelta(seconds=20), "undo", phone, "effort", eid)
                    continue
                logged.append((when, eid, metrics))
        for when, eid, metrics in rng.sample(logged, k=min(len(logged), rng.choice((0, 0, 1, 2)))):
            fixed = {**metrics, "reps": {"value": metrics["reps"]["value"] + 1}}
            self.author(when + timedelta(minutes=1), phone, {"intent": "edit", "record": ["effort", eid],
                                                              "fields": {"metrics": fixed}})
        ended = began + timedelta(minutes=minute + 3)
        self.author(ended, phone, {"intent": "edit", "record": ["session", plan_id],
                                   "fields": {"ended_at": stamp(ended, self.offset(d))}})
        self.workout(d, began, ended, "weightlifting_msk", logged_by=plan_id)

    def other(self, d: int, phone: str) -> None:
        rng = self.rng
        sport = rng.choices([s for s, _ in OTHER], [w for _, w in OTHER])[0]
        began = self.at(d, rng.choice((6.5, 12, 17.5)))
        minutes = rng.randint(25, 75)
        ended = began + timedelta(minutes=minutes)
        logged = None
        if rng.random() < 0.4:
            logged = self.id("s")
            metrics = {"duration": {"value": minutes, "unit": "min"}}
            self.author(ended, phone, {"intent": "log_session", "record": {
                "id": logged, "name": sport, "started_at": stamp(began, self.offset(d)),
                "ended_at": stamp(ended, self.offset(d)), "metrics": metrics}})
            self.p.local_dates[f"session/{logged}"] = began.astimezone(timezone(self.offset(d))).date().isoformat()
        self.workout(d, began, ended, sport, logged_by=logged)

    def workout(self, d: int, began: datetime, ended: datetime, sport: str, logged_by: str | None) -> None:
        """One real workout, seen by WHOOP (mostly), copied to Apple Health (often) and maybe logged by hand."""
        rng = self.rng
        truth = {"start": began, "end": ended, "sport": sport, "logged_by": logged_by, "sources": []}
        off = self.offset(d)
        distance = round(rng.uniform(4000, 14000), 1) if sport in ("running", "cycling") else None
        if rng.random() < 0.9:
            wid = self.uuid()
            scored = rng.random() > 0.03
            record = {"id": wid, "user_id": 1, "created_at": stamp(ended, off), "updated_at": stamp(ended, off),
                      "start": stamp(began + timedelta(minutes=rng.randint(0, 3)), off),
                      "end": stamp(ended - timedelta(minutes=rng.randint(0, 3)), off),
                      "timezone_offset": _offset_text(off), "sport_name": sport,
                      "score_state": "SCORED" if scored else "PENDING_SCORE",
                      "score": _workout_score(rng, distance if rng.random() < 0.3 else None) if scored else None}
            self.add(ended + timedelta(hours=1), "ingest", "laptop", "whoop", {"workout": [record]})
            truth["sources"].append(("whoop", wid))
            if rng.random() < 0.05:
                later = ended + timedelta(days=rng.randint(1, 4))
                rescored = {**record, "updated_at": stamp(later, off), "score_state": "SCORED",
                            "score": _workout_score(rng, distance)}
                self.add(later, "ingest", "laptop", "whoop", {"workout": [rescored]})
                self.p.revised.append(f"whoop:workout:{wid}")
        if rng.random() < 0.6:
            hid = self.id("h")
            raw = {"duration_s": (ended - began).total_seconds(), "heart_rate_avg_bpm": rng.randint(110, 160)}
            if distance:
                raw["distance_m"] = distance
            sample = {"source": "apple-health", "source_id": hid, "type": "HKWorkoutTypeIdentifier",
                      "start": stamp(began, timedelta(0)), "end": stamp(ended, timedelta(0)), "value": HEALTHKIT[sport],
                      "raw": raw}
            self.add(ended + timedelta(hours=2), "ingest", self.phone(d), "apple-health", [sample])
            truth["sources"].append(("apple-health", hid))
        if truth["sources"] or logged_by:
            self.p.workouts.append(truth)

    def night(self, d: int) -> None:
        rng = self.rng
        off = self.offset(d)
        fell = self.at(d, 23 + rng.uniform(-1, 1))
        woke = fell + timedelta(hours=rng.uniform(6, 8.5))
        cycle, sleep = 10_000 + d, self.uuid()
        sleep_record = {"id": sleep, "cycle_id": cycle, "user_id": 1, "created_at": stamp(woke, off),
                        "updated_at": stamp(woke, off), "start": stamp(fell, off), "end": stamp(woke, off),
                        "timezone_offset": _offset_text(off), "nap": False, "score_state": "SCORED",
                        "score": _sleep_score(rng, (woke - fell).total_seconds())}
        recovery = {"cycle_id": cycle, "sleep_id": sleep, "user_id": 1, "created_at": stamp(woke, off),
                    "updated_at": stamp(woke, off), "score_state": "SCORED", "score": {
                        "user_calibrating": False, "recovery_score": rng.randint(20, 95),
                        "resting_heart_rate": rng.randint(45, 60), "hrv_rmssd_milli": round(rng.uniform(40, 110), 1)}}
        cycle_record = {"id": cycle, "user_id": 1, "created_at": stamp(fell, off), "updated_at": stamp(woke, off),
                        "start": stamp(fell, off), "end": None, "timezone_offset": _offset_text(off),
                        "score_state": "SCORED", "score": {"strain": round(rng.uniform(4, 18), 1),
                                                           "kilojoule": round(rng.uniform(7000, 14000), 1),
                                                           "average_heart_rate": rng.randint(60, 80),
                                                           "max_heart_rate": rng.randint(140, 185)}}
        batch = {"sleep": [sleep_record], "recovery": [recovery], "cycle": [cycle_record]}
        self.add(woke + timedelta(hours=1), "ingest", "laptop", "whoop", batch)
        if rng.random() < 0.05:
            later = woke + timedelta(days=rng.randint(1, 4))
            batch = {"sleep": [{**sleep_record, "updated_at": stamp(later, off),
                                "score": _sleep_score(rng, (woke - fell).total_seconds())}]}
            self.add(later, "ingest", "laptop", "whoop", batch)
            self.p.revised.append(f"whoop:sleep:{sleep}")

    def weigh_in(self, d: int) -> None:
        rng = self.rng
        at = self.at(d, 7.2)
        weight = round(80 + 2 * rng.gauss(0, 1) - d * 0.002, 1)
        samples = [{"source": "hume", "source_id": f"hume-{d}-{t}", "type": t, "start": stamp(at, timedelta(0)),
                    "end": stamp(at, timedelta(0)), "value": v, "unit": u}
                   for t, v, u in (("HKQuantityTypeIdentifierBodyMass", weight, "kg"),
                                   ("HKQuantityTypeIdentifierBodyFatPercentage", round(rng.uniform(14, 19), 1), "%"))]
        self.add(at, "ingest", self.phone(d), "hume", samples)


def _offset_text(off: timedelta) -> str:
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _workout_score(rng: random.Random, distance: float | None) -> dict:
    zones = {f"zone_{n}_milli": rng.randint(0, 900_000) for n in ("zero", "one", "two", "three", "four", "five")}
    score = {"strain": round(rng.uniform(5, 16), 1), "average_heart_rate": rng.randint(110, 160),
             "max_heart_rate": rng.randint(160, 190), "kilojoule": round(rng.uniform(800, 3500), 1),
             "percent_recorded": 100.0, "zone_durations": zones}
    if distance is not None:
        score["distance_meter"] = distance
    return score


def _sleep_score(rng: random.Random, seconds: float) -> dict:
    ms = int(seconds * 1000)
    light, deep, rem = int(ms * 0.5), int(ms * 0.2), int(ms * 0.22)
    return {"stage_summary": {"total_in_bed_time_milli": ms, "total_awake_time_milli": ms - light - deep - rem,
                              "total_no_data_time_milli": 0, "total_light_sleep_time_milli": light,
                              "total_slow_wave_sleep_time_milli": deep, "total_rem_sleep_time_milli": rem,
                              "sleep_cycle_count": rng.randint(3, 6), "disturbance_count": rng.randint(2, 15)},
            "sleep_needed": {"baseline_milli": 27_000_000, "need_from_sleep_debt_milli": 0,
                             "need_from_recent_strain_milli": 0, "need_from_recent_nap_milli": 0},
            "respiratory_rate": round(rng.uniform(13, 17), 1),
            "sleep_performance_percentage": rng.randint(60, 100)}


def persona(seed: int = 1, days: int = 365) -> Persona:
    return _Gen(seed, days).run()


def play(world: World, p: Persona, until: int | None = None) -> None:
    """Drive a world through the persona's steps, true time following each step."""
    for name in ("laptop", "phone"):
        if name not in world.replicas:
            world.replica(name)
    for step in p.steps[:until]:
        world.now = max(world.now, step.at)
        if step.op == "author":
            world.author(step.replica, *step.args)
        elif step.op == "ingest":
            world.ingest(step.replica, *step.args)
        elif step.op == "sync":
            world.sync(step.replica, step.args[0])
        elif step.op == "clock":
            world.clock(step.replica).skew = step.args[0]
        elif step.op == "undo":
            target = next(c for c in reversed(world.changes.values())
                          if c.replica == step.replica and any(w.record == key(*step.args) for w in c.writes))
            world.author(step.replica, {"intent": "undo", "change": target.id})
        elif step.op == "device":
            world.replica(step.replica)
