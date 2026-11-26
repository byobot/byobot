"""A phone's week sent through the hub and the cloud folder at once lands as the same store as through the hub alone."""

import random
import threading

import pytest

from byobot import serve
from byobot.cloud.folder import LocalFolder
from byobot.cloud.keeper import Keeper
from byobot.cloud.relay import Relay
from byobot.core import hlc, relay as rules
from byobot.core.store import Store
from byobot.util import uuid7

from test_cloud_keeper import PHONE, Clock
from test_serve import Client, _pair

ME = {"kind": "human", "id": "me"}
START = 1795400000000


class Phone:
    """Authors a week as the app does: a session a day with its sets, edits, an undo, a clash and weigh-ins."""

    def __init__(self, rng):
        self.rng, self.ms = rng, START

    def event(self, kind, rid, payload, op="put", prev=()):
        self.ms += self.rng.randrange(1000, 60000)
        e = {"id": uuid7(), "schema_version": 1, "record": {"kind": kind, "id": rid}, "op": op,
             "hlc": hlc.format((self.ms, 0)), "ts": "2026-11-23T08:00:00-05:00", "device_id": PHONE, "actor": ME,
             "prev": list(prev)}
        return {**e, "kind_version": 1, "payload": payload} if op == "put" else {**e, "payload": payload}

    def week(self):
        """What the phone sends, in the order it was logged: ("events", [...]) and ("samples", {...})."""
        out, weights = [], []
        for day in range(7):
            date = f"2026-11-{23 + day:02d}"
            sid = uuid7()
            session = self.event("session", sid, {"name": f"Day {day}", "modality": "self_reported",
                                                  "time": {"start": f"{date}T07:30:00-05:00"}})
            sets = []
            for n in range(4):
                sets.append(self.event("effort", uuid7(), {
                    "activity_id": "barbell_back_squat", "modality": "self_reported",
                    "time": {"start": f"{date}T07:{40 + n}:00-05:00"},
                    "links": {"part_of": {"kind": "session", "id": sid}},
                    "metrics": {"reps": {"value": 5}, "load": {"value": 100 + 5 * n, "unit": "kg"}}}))
            later = [self.event("effort", sets[1]["record"]["id"],
                                {**sets[1]["payload"], "metrics": {"reps": {"value": 4}}}, prev=[sets[1]["id"]])]
            if day == 2:
                later.append(self.event("effort", sets[3]["record"]["id"], {"reason": "Logged twice."}, op="retract",
                                        prev=[sets[3]["id"]]))
            if day == 4:  # two edits of one set from the same version: the clash shows
                later += [self.event("effort", sets[0]["record"]["id"], {**sets[0]["payload"], "notes": text},
                                     prev=[sets[0]["id"]]) for text in ("easy", "hard")]
            finished = self.event("session", sid, {**session["payload"], "time": {
                "start": f"{date}T07:30:00-05:00", "end": f"{date}T08:20:00-05:00"}}, prev=[session["id"]])
            out.append(("events", [session, *sets]))
            out.append(("events", [*later, finished]))
            weight = {"source": "apple_health", "source_id": f"W{day}", "type": "HKQuantityTypeIdentifierBodyMass",
                      "start": f"{date}T06:30:00-05:00", "end": f"{date}T06:30:00-05:00", "value": 80 + day / 10, "unit": "kg"}
            weights.append(weight)
            out.append(("samples", {"samples": [weight]}))
            if day == 5:
                out.append(("samples", {"deleted": [{"source": "apple_health", "source_id": "W3"}]}))
        return out


def held(store):
    """The store as a reader sees it: the phone's events, each record's current version and clash, and the samples.
    Ids a store derives from its own key differ between stores, so samples are compared by what they hold."""
    return (sorted(e["id"] for e in store.events() if e["device_id"] == PHONE),
            [(r["kind"], r["id"], r["conflict"], r["event"]["id"]) for r in store.records()
             if r["kind"] != "source_record"],
            sorted(r["payload"]["source_id"] for r in store.records("source_record")),
            store.rows("SELECT code, value FROM observations ORDER BY value"),
            store.rows("SELECT id, reps, load_kg FROM strength_sets ORDER BY id"))


def hub_alone(path, week):
    with Store(path) as s:
        for what, body in week:
            if what == "events":
                rules.push_events(s, PHONE, {"events": body})
            else:
                rules.accept_samples(s, body)
        return held(s)


@pytest.fixture
def hub(byobot_home):
    srv, info = serve.make_server("127.0.0.1", 0, pair=True, log=lambda *_: None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    client = Client(info["port"], info["fingerprint"])
    _pair(client, info)
    yield client
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("seed", range(12))
def test_a_week_through_both_routes_lands_as_through_the_hub(hub, store, tmp_path, seed):
    rng = random.Random(seed)
    week = Phone(rng).week()
    wanted = hub_alone(tmp_path / "hub-alone.db", week)

    folder = LocalFolder(tmp_path / "cloud", store.store_id)
    keeper = Keeper.setup(store, folder, Clock())
    phone = Relay.join(folder, keeper.join_code()["link"], PHONE)
    keeper.sync()
    phone.sync()

    def by_hub(what, body):
        status, answer = hub.call("POST", f"/v1/{what}", {"events": body} if what == "events" else body)
        assert status == 200, answer

    def by_cloud(what, body):
        phone.log(body) if what == "events" else phone.send_samples(body)

    for what, body in week:
        route = rng.choice(["hub", "cloud", "both", "both"])
        if route in ("hub", "both"):
            by_hub(what, body)
        if route in ("cloud", "both"):
            by_cloud(what, body)
        if rng.random() < 0.5:
            phone.sync()
        if rng.random() < 0.4:
            keeper.sync()
    for _ in range(3):
        phone.sync()
        keeper.sync()
    phone.sync()
    assert phone.events == {} and phone.samples == [] and phone.sent == {}
    assert held(store) == wanted


@pytest.mark.xfail(strict=True, reason="a deletion that lands before its sample is dropped, so the sample stays")
def test_a_deletion_by_one_route_before_its_sample_by_the_other(hub, store, tmp_path):
    folder = LocalFolder(tmp_path / "cloud", store.store_id)
    keeper = Keeper.setup(store, folder, Clock())
    phone = Relay.join(folder, keeper.join_code()["link"], PHONE)
    keeper.sync()
    phone.sync()
    weight = {"source": "apple_health", "source_id": "W1", "type": "HKQuantityTypeIdentifierBodyMass",
              "start": "2026-11-23T06:30:00-05:00", "end": "2026-11-23T06:30:00-05:00", "value": 80.0, "unit": "kg"}
    phone.send_samples({"samples": [weight]})
    phone.sync()
    hub.call("POST", "/v1/samples", {"deleted": [{"source": "apple_health", "source_id": "W1"}]})
    keeper.sync()
    assert store.records("source_record") == [] and store.rows("SELECT code FROM observations") == []
