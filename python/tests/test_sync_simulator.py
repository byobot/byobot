"""A seeded network: the laptop's hub, phones that sync with it, and the faults between them.

Phones keep a logbook of their own and write through the same commands the laptop does. They push over the real
HTTP handlers and read back the laptop's logbook. Requests and answers get lost, deliveries arrive twice and out of
order, clocks drift, phones go offline, the hub restarts, and the laptop erases. Once the faults stop and everything syncs, every logbook must hold the same events and records,
nothing a phone wrote may be missing unless the hub refused it and said so, and every record edited concurrently
must show its clash.

A failure names its seed: BYOBOT_SIM_SEED=<seed> python -m pytest tests/test_sync_simulator.py reruns just it.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from byobot import serve, web
from byobot.core import commands
from byobot.core.store import Store
from byobot.util import ByobotError

from helpers import ME

START = datetime(2026, 11, 23, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
SOAK = bool(os.environ.get("BYOBOT_SOAK"))
SEEDS = [int(os.environ["BYOBOT_SIM_SEED"])] if os.environ.get("BYOBOT_SIM_SEED") else range(200 if SOAK else 10)
STEPS = 300 if SOAK else 80


class Net:
    """The seeded clock, ids and faults every device shares."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.now = START
        self.faults = True
        self.counter = 0

    def chance(self, p: float) -> bool:
        return self.faults and self.rng.random() < p

    def ids(self, n: int):
        def new_id() -> str:
            self.counter += 1
            return str(uuid.UUID(int=(n << 96) | (7 << 76) | (2 << 62) | self.counter))
        return new_id


class Hub:
    """The laptop: its logbook and `byobot serve` on it."""

    def __init__(self, net: Net, root: Path):
        self.net, self.db, self.skew = net, root / "hub.db", timedelta(0)
        self.store = self._open()
        self.server = None
        self.start(pair=True)

    def _open(self) -> Store:
        return Store(self.db, wall=lambda: self.net.now + self.skew, new_id=self.net.ids(1))

    def start(self, pair=False):
        srv, info = serve.make_server("127.0.0.1", 0, pair=pair, tls=False, db=self.db, log=lambda *_: None)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.server, self.port = srv, info["port"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def restart(self):
        self.stop()
        self.store.close()
        self.store = self._open()
        self.start()

    def call(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def pair(self, device_id: str) -> str:
        self.server.pairing = web.Pairing()
        status, body = self.call("POST", "/v1/pair", {"code": self.server.pairing.code, "device_id": device_id,
                                                       "name": "Example iPhone"})
        assert status == 200, body
        return body["token"]


class Phone:
    """A phone with its own logbook, paired with the hub."""

    def __init__(self, net: Net, hub: Hub, root: Path, n: int):
        self.net, self.hub, self.n = net, hub, n
        self.device_id = f"0193a0b0-0000-7000-8000-00000000ab0{n}"
        self.path, self.skew = root / f"phone{n}.db", timedelta(minutes=net.rng.randint(-180, 180))
        self.store = self._open()
        self.online, self.acked, self.cursor = True, set(), 0
        self.rejected: dict[str, str] = {}
        self.token = hub.pair(self.device_id)

    def _open(self) -> Store:
        return Store(self.path, device_id=self.device_id, wall=lambda: self.net.now + self.skew,
                     new_id=self.net.ids(10 + self.n))

    def restart(self):
        self.store.close()
        self.store = self._open()

    def mine(self) -> list[dict]:
        return [e for e in self.store.events() if e["device_id"] == self.device_id]

    # -- sending -----------------------------------------------------------------

    def push(self, settling=False):
        if not self.online:
            return
        todo = [e for e in self.mine() if e["id"] not in self.acked and e["id"] not in self.rejected
                and "redacted_by" not in e]  # an erase that reached the phone first drops what it hadn't sent
        if not todo:
            return
        rng = self.net.rng
        batch = todo if settling else rng.sample(todo, rng.randint(1, len(todo)))
        if self.net.chance(0.3):
            batch = batch + rng.sample(batch, rng.randint(1, len(batch)))
            rng.shuffle(batch)
        if self.net.chance(0.1):
            return  # the request never left
        status, body = self.hub.call("POST", "/v1/events", {"events": batch}, self.token)
        assert status == 200, body
        if self.net.chance(0.1):
            return  # the answer was lost
        for event, result in zip(batch, body["results"]):
            if result["status"] == "rejected":
                self.rejected[event["id"]] = result.get("reason", "")
            else:
                self.acked.add(event["id"])

    # -- reading -----------------------------------------------------------------

    def pull(self):
        if not self.online:
            return
        if self.net.chance(0.1):
            return
        with self.hub.store.snapshot():
            self._take(self.hub.store.events(since=self.cursor))
            self.cursor = self.hub.store.position()

    def _take(self, events):
        events = list(events)
        if self.net.chance(0.2):
            self.net.rng.shuffle(events)
        outcomes = self.store.accept_many(events)
        bad = [(e["id"], o) for e, o in zip(events, outcomes) if o[0] == "rejected"]
        assert bad == [], bad


# -- what people do ----------------------------------------------------------------

def effort(rng, at):
    return {"activity_id": "barbell_back_squat",
            "time": {"start": at.isoformat(timespec="seconds")},
            "metrics": {"reps": {"value": rng.randint(1, 12)}, "load": {"value": rng.randint(20, 140), "unit": "kg"}}}


def act(store: Store, rng: random.Random, now: datetime, laptop: bool):
    """One thing a person does in a logbook, picked at random from what it holds."""
    live = [(r["kind"], r["id"]) for r in store.records() if r["kind"] in ("effort", "session")]
    clashes = store.conflicts()
    choice = rng.choices(["log", "session", "edit", "remove", "resolve", "undo", "erase"],
                         [5, 1, 5, 1, 3 if clashes else 0, 1, 1 if laptop else 0])[0]
    try:
        if choice == "log":
            commands.log(store, ME, [{"kind": "effort", "payload": effort(rng, now)}])
        elif choice == "session":
            commands.log(store, ME, [{"kind": "session", "payload": {
                "name": "Legs", "time": {"start": now.isoformat(timespec="seconds")}}}])
        elif choice == "edit" and live:
            kind, rid = rng.choice(live)
            patch = {"notes": f"note {rng.randint(0, 99)}"} if kind == "session" or rng.random() < 0.5 else {
                "metrics": {"reps": {"value": rng.randint(1, 12)}}}
            commands.edit(store, ME, kind, rid, patch)
        elif choice == "remove" and live:
            commands.remove(store, ME, *rng.choice(live))
        elif choice == "resolve":
            clash = rng.choice(clashes)
            pick = rng.choice([v["id"] for v in clash["versions"]] + ["remove"])
            commands.resolve(store, ME, clash["kind"], clash["id"], pick)
        elif choice == "undo":
            mine = [e for e in store.events() if e["device_id"] == store.device_id and e["op"] in ("put", "retract")]
            if mine:
                commands.undo(store, ME, rng.choice(mine[-5:])["id"])
        elif choice == "erase" and live:
            commands.erase(store, ME, *rng.choice(live))
    except ByobotError:
        pass  # a step the person couldn't take is refused, as the app would show it


# -- the run -----------------------------------------------------------------------

def view(store: Store, unsent=frozenset()):
    """What a reader learns from a logbook: every event, and each record's current version and clash."""
    return (sorted(e["id"] for e in store.events() if e["id"] not in unsent),
            [(r["kind"], r["id"], r["conflict"], r["event"]["id"]) for r in store.records()],
            sorted((c["kind"], c["id"], sorted(v["id"] for v in c["versions"])) for c in store.conflicts()))


def versions(store: Store) -> dict[tuple, set]:
    """Each record's distinct versions that no later write names as seen. Two that say the same thing don't clash."""
    seen, tips = set(), {}
    for e in store.events():
        seen.update(e.get("prev") or [])
    for e in store.events():
        if e["op"] in ("put", "retract") and e["id"] not in seen and "redacted_by" not in e:
            said = json.dumps([e["op"], e["payload"] if e["op"] == "put" else None], sort_keys=True)
            tips.setdefault((e["record"]["kind"], e["record"]["id"]), set()).add(said)
    return tips


def run(seed: int, root: Path):
    net = Net(seed)
    hub = Hub(net, root)
    phones = [Phone(net, hub, root, n) for n in range(net.rng.randint(1, 3))]
    try:
        for _ in range(STEPS):
            net.now += timedelta(minutes=net.rng.randint(1, 90))
            phone = net.rng.choice(phones)
            what = net.rng.choices(["phone", "laptop", "push", "pull", "offline", "skew", "restart"],
                                   [6, 2, 4, 4, 1, 1, 0.5])[0]
            if what == "phone":
                act(phone.store, net.rng, net.now + phone.skew, laptop=False)
            elif what == "laptop":
                act(hub.store, net.rng, net.now + hub.skew, laptop=True)
            elif what == "push":
                phone.push()
            elif what == "pull":
                phone.pull()
            elif what == "offline":
                phone.online = not phone.online
            elif what == "skew":
                target = net.rng.choice([hub, phone])
                target.skew = timedelta(minutes=net.rng.randint(-600, 600))
            elif net.rng.random() < 0.5:
                hub.restart()
            else:
                phone.restart()
        settle(net, hub, phones)
        check(hub, phones)
    finally:
        hub.stop()
        for p in phones:
            p.store.close()
        hub.store.close()


def settle(net: Net, hub: Hub, phones: list[Phone]):
    net.faults = False
    for p in phones:
        p.online = True
    for _ in range(6):
        for p in phones:
            p.push(settling=True)
        for p in phones:
            p.pull()


def check(hub: Hub, phones: list[Phone]):
    want = view(hub.store)
    held = set(want[0])
    for p in phones:
        unsent = {e["id"] for e in p.mine() if e["id"] not in held and "redacted_by" in e}
        lost = [e["id"] for e in p.mine() if e["id"] not in held and e["id"] not in unsent]
        assert lost == [], f"phone {p.n} wrote events the hub never got: {lost} {p.rejected}"
        assert view(p.store, unsent) == want, f"phone {p.n} doesn't match the hub"
    clashing = {(c["kind"], c["id"]) for c in hub.store.conflicts()}
    for record, tips in versions(hub.store).items():
        if len(tips) > 1 and not hub.store.record(*record)["erased"]:
            assert record in clashing, f"{record} has concurrent versions {sorted(tips)} but shows no clash"
        if record in clashing:
            assert len(tips) > 1, f"{record} shows a clash with one current version"


@pytest.mark.parametrize("seed", SEEDS)
def test_every_device_converges_and_nothing_is_lost(tmp_path, seed):
    try:
        run(seed, tmp_path)
    except AssertionError as e:
        raise AssertionError(f"seed {seed}: {e}\nrerun: BYOBOT_SIM_SEED={seed} python -m pytest "
                             "tests/test_sync_simulator.py") from None
