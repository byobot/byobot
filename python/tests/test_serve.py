"""``byobot serve``: the LAN sync endpoint the app pairs with, over real HTTPS."""

import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from byobot import serve
from byobot.core import commands, hlc
from byobot.core.contract import sync_errors
from byobot.core.store import Store
from byobot.util import uuid7

SYNC = json.loads((Path(__file__).resolve().parents[2] / "schema" / "fixtures" / "v1" / "sync.json").read_text())
PHONE = "0193a0b0-0000-7000-8000-00000000ab01"
ME = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "example-coach"}


# -- the shared protocol fixtures ---------------------------------------------

@pytest.mark.parametrize("item", SYNC["cases"], ids=[c["name"] for c in SYNC["cases"]])
def test_sync_fixture(item):
    errors = sync_errors(item["def"], item["data"])
    assert (not errors) == item["valid"], errors


@pytest.mark.parametrize("item", SYNC["links"], ids=[c["name"] for c in SYNC["links"]])
def test_pairing_link_fixture(item):
    if not item["valid"]:
        with pytest.raises((ValueError, KeyError)):
            parsed = serve.parse_pairing_link(item["link"])
            if parsed["v"] != serve.PROTOCOL:
                raise ValueError("protocol")
        return
    parsed = serve.parse_pairing_link(item["link"])
    assert parsed["v"] == serve.PROTOCOL
    assert {k: parsed[k] for k in ("hosts", "port", "code", "fp")} == item["expect"]


def test_pairing_link_round_trips():
    fp = hashlib.sha256(b"x").hexdigest()
    link = serve.pairing_link(["host.local", "192.0.2.2"], 8765, "012345", fp)
    assert serve.parse_pairing_link(link) == {"v": 1, "hosts": ["host.local", "192.0.2.2"], "port": 8765,
                                             "code": "012345", "fp": fp}


# -- pairing codes -------------------------------------------------------------

def test_pairing_code_is_single_use():
    p = serve.Pairing()
    assert len(p.code) == 6 and p.code.isdigit() and p.open
    assert p.check(p.code[:3] + "-" + p.code[3:])
    assert not p.open and not p.check(p.code)


def test_pairing_closes_after_too_many_wrong_codes():
    p = serve.Pairing()
    wrong = "000000" if p.code != "000000" else "111111"
    for _ in range(serve.PAIRING_TRIES):
        assert not p.check(wrong)
    assert not p.open and not p.check(p.code)


def test_pairing_expires(monkeypatch):
    p = serve.Pairing()
    monkeypatch.setattr(serve.time, "monotonic", lambda: p.expires_at + 1)
    assert not p.open and not p.check(p.code)


# -- what the phone pulls ------------------------------------------------------

LOWER, UPPER, LATER, PLAN, DONE = (f"0193a0b0-0000-7000-8000-0000000001{i:02d}" for i in range(5))


def write(store, *records, actor=AGENT):
    return commands.log(store, actor, [{"kind": k, "id": i, "payload": p} for k, i, p in records])


def lift(day, name, location="example-gym"):
    return {"scheduled_on": day, "name": name, "location_id": location, "links": {"part_of": {"kind": "plan", "id": PLAN}},
            "blocks": [{"rounds": 3, "efforts": [{"activity_id": "barbell_back_squat",
                                                  "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}}]}]}


def seed(store):
    write(store,
          ("activity", "barbell_back_squat", {"name": "Barbell back squat", "targets": {"main": ["quads"]}}),
          ("location", "example-gym", {"name": "Example gym"}),
          ("setting", "equipment", {"value": ["barbell", "rack"]}),
          ("plan", PLAN, {"name": "Block 1", "starts_on": "2026-11-23"}),
          ("planned_session", LOWER, lift("2026-11-23", "Lower A")),
          ("planned_session", UPPER, lift("2026-11-25", "Upper A")),
          ("planned_session", LATER, lift("2027-01-10", "Much later")))
    answers = {"kind": "planned_session", "id": LOWER}
    write(store, ("session", DONE, {"name": "Lower A", "location_id": "example-gym", "links": {"answers": answers},
                                    "time": {"start": "2026-11-23T07:30:00-05:00"}}),
          ("effort", None, {"activity_id": "barbell_back_squat", "round": 1, "time": {"start": "2026-11-23T07:35:00-05:00"},
                            "links": {"part_of": {"kind": "session", "id": DONE},
                                      "answers": {**answers, "part": f"{LOWER}.0.0"}},
                            "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}}), actor=ME)


def test_plan_window_is_contract_shaped(store):
    seed(store)
    window = serve.plan_window(store, "2026-11-22", "2026-12-05")
    assert sync_errors("plan_window", window) == []
    assert [s["id"] for s in window["planned_sessions"]] == [LOWER, UPPER]
    for entry in window["planned_sessions"]:
        assert entry["payload"] == store.get("planned_session", entry["id"])["payload"]  # exactly as authored
        assert entry["prev"] == store.record("planned_session", entry["id"])["tips"]
    assert [p["id"] for p in window["plans"]] == [PLAN]
    assert [a["id"] for a in window["activities"]] == ["barbell_back_squat"]
    assert [s["id"] for s in window["settings"]] == ["equipment"]
    assert [s["id"] for s in window["sessions"]] == [DONE]
    [effort] = window["efforts"]
    assert effort["payload"]["round"] == 1 and effort["payload"]["metrics"]["load"] == {"value": 100, "unit": "kg"}


def test_plan_window_sends_the_library(byobot_home, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "activities").mkdir(parents=True)
    (lib / "vocabulary.json").write_text("{}")
    dip = {"id": "dip", "name": "Dip", "targets": {"main": ["triceps"]},
           "resistance_curve": {"stretch": 3, "middle": 2, "contract": 1}}
    (lib / "activities" / "dip.json").write_text(json.dumps(dip))
    monkeypatch.setenv("BYOBOT_LIBRARY", str(lib))
    with Store(byobot_home / "lib.db") as s:
        window = serve.plan_window(s, "2026-11-22", "2026-11-28")
    assert window["activities"] == [{"id": "dip", "payload": {k: v for k, v in dip.items() if k != "id"}, "prev": [],
                                     "library": True}]
    assert sync_errors("plan_window", window) == []


def test_plan_window_follows_replanning_undo_and_rearranging(store):
    seed(store)
    replanned = commands.replan(store, AGENT, LOWER, lift("2026-11-23", "Lower A (deload)"))

    def names():
        window = serve.plan_window(store, "2026-11-01", "2026-12-31")
        return {s["id"]: s["payload"]["name"] for s in window["planned_sessions"]}

    assert names()[LOWER] == "Lower A (deload)"
    commands.undo(store, ME, replanned["id"])
    assert names()[LOWER] == "Lower A"
    rid = store.contract.derived("overlay", ["rearrange", "planned_session", LOWER])
    store.write(ME, [{"kind": "overlay", "id": rid, "op": "put", "payload": {
        "effect": "rearrange", "target": {"kind": "planned_session", "id": LOWER}, "order": [f"{LOWER}.0.0"]}}])
    window = serve.plan_window(store, "2026-11-01", "2026-12-31")
    assert [o["payload"]["target"]["id"] for o in window["overlays"]] == [LOWER]


def test_plan_window_shows_a_conflict(store):
    seed(store)
    tips, payload = store.record("planned_session", UPPER)["tips"], store.get("planned_session", UPPER)["payload"]
    for name in ("Upper A (phone)", "Upper A (coach)"):
        store.write(ME, [{"kind": "planned_session", "id": UPPER, "op": "put", "prev": tips,
                          "payload": {**payload, "name": name}}])
    [entry] = [s for s in serve.plan_window(store, "2026-11-22", "2026-12-05")["planned_sessions"] if s["id"] == UPPER]
    assert entry["conflict"] is True and entry["prev"] == [store.record("planned_session", UPPER)["current"]]
    store.write(ME, [{"kind": "planned_session", "id": UPPER, "op": "put", "prev": entry["prev"],
                      "payload": {**entry["payload"], "notes": "from the phone"}}])
    assert store.record("planned_session", UPPER)["conflict"]  # a phone edit keeps the conflict


def test_plan_window_outside_the_dates_is_empty(store):
    seed(store)
    window = serve.plan_window(store, "2030-01-01", "2030-01-07")
    assert window["planned_sessions"] == [] and window["efforts"] == [] and window["sessions"] == []


def test_plan_window_brings_the_last_two_times_at_each_location(store):
    seed(store)
    for i, (day, location) in enumerate([("10", "home"), ("12", "home"), ("14", "home"), ("15", "hotel-gym")]):
        sid = f"0193a0b0-0000-7000-8000-0000000005{i:02d}"
        write(store, ("session", sid, {"name": "Squats", "location_id": location,
                                       "time": {"start": f"2026-11-{day}T08:00:00Z"}}),
              *[("effort", f"0193a0b0-0000-7000-8000-0000000006{i}{n}", {
                  "activity_id": "barbell_back_squat", "round": n, "time": {"start": f"2026-11-{day}T08:0{n}:00Z"},
                  "links": {"part_of": {"kind": "session", "id": sid}},
                  "metrics": {"load": {"value": 90 + i, "unit": "kg"}}}) for n in (1, 2)], actor=ME)
    window = serve.plan_window(store, "2026-11-25", "2026-11-30")
    ours = [e["id"][-2:] for e in window["efforts"] if e["id"].startswith("0193a0b0-0000-7000-8000-0000000006")]
    assert ours == ["11", "12", "21", "22", "31", "32"] and len(window["efforts"]) == 7  # and the seeded gym visit
    assert [p["id"] for p in window["planned_sessions"]] == [UPPER]


# -- over HTTPS ----------------------------------------------------------------

class Client:
    """A minimal client that pins the server's certificate, as the app does."""

    def __init__(self, port, fp, token=None):
        self.port, self.fp, self.token = port, fp, token

    def call(self, method, path, body=None, token=None):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # trust comes from the pinned fingerprint below
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=10)
        try:
            conn.connect()
            served = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            assert served == self.fp, "certificate does not match the pinned fingerprint"
            headers = {"Content-Type": "application/json"}
            tok = token if token is not None else self.token
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()


@pytest.fixture
def server(byobot_home):
    logs = []
    srv, info = serve.make_server("127.0.0.1", 0, pair=True, log=logs.append)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, info, Client(info["port"], info["fingerprint"]), logs
    srv.shutdown()
    srv.server_close()


def _pair(client, info, device_id=PHONE):
    status, body = client.call("POST", "/v1/pair", {"code": info["code"], "device_id": device_id, "name": "Test phone"})
    assert status == 200, body
    assert sync_errors("pair_response", body) == []
    client.token = body["token"]
    return body


CLOCK = [1795400000000]


def _event(kind, rid, payload=None, op="put", prev=(), device_id=PHONE, actor=ME, at=None, reverts=None):
    """An event as the app authors it."""
    CLOCK[0] += 1000
    ms = hlc.millis(datetime.fromisoformat(at.replace("Z", "+00:00"))) if at else CLOCK[0]
    event = {"id": uuid7(), "schema_version": 1, "record": {"kind": kind, "id": rid}, "op": op,
             "hlc": hlc.format((ms, 0)), "ts": at or "2026-11-23T08:00:00-05:00", "device_id": device_id,
             "actor": actor, "prev": list(prev)}
    if reverts:
        event["reverts"] = reverts
    return {**event, "kind_version": 1, "payload": payload} if op == "put" else {**event, "payload": payload or {}}


def db(byobot_home):
    return Store(byobot_home / "byobot.db")


def test_certificate_is_made_once_and_kept(server, byobot_home):
    _, info, _, _ = server
    cert = byobot_home / "serve" / "cert.pem"
    assert cert.is_file() and (byobot_home / "serve" / "key.pem").is_file()
    assert serve.fingerprint(cert.read_bytes()) == info["fingerprint"]
    with db(byobot_home) as s:
        _, _, again = serve.ensure_certificate(s.device_id)
    assert again == info["fingerprint"]
    assert info["link"].startswith("byobot://pair?v=1&") and serve.parse_pairing_link(info["link"])["fp"] == again


def test_plain_http_is_refused(server):
    _, info, _, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
    with pytest.raises((http.client.HTTPException, ConnectionError, socket.timeout, OSError)):
        conn.request("GET", "/v1/hello")
        conn.getresponse().read()
    conn.close()


def test_hello_needs_no_token_and_names_the_store(server, byobot_home):
    _, info, client, _ = server
    status, body = client.call("GET", "/v1/hello")
    assert status == 200 and body["pairing_open"] is True and body["server"]["device_id"] == info["device_id"]
    with db(byobot_home) as s:
        assert body["server"]["store_id"] == s.store_id
    assert sync_errors("hello", body) == []
    assert client.call("GET", "/v0/hello")[0] == 404  # an app on the old protocol is told nothing it would misread


def test_everything_else_needs_a_token(server):
    _, _, client, _ = server
    for method, path in (("GET", "/v1/status"), ("GET", "/v1/plan"), ("GET", "/v1/events"), ("POST", "/v1/events"),
                         ("POST", "/v1/samples")):
        status, body = client.call(method, path, {"events": [], "samples": []} if method == "POST" else None,
                                   token="not-a-token")
        assert status == 401 and body["error"] == "unauthorized", path
        assert sync_errors("error", body) == []


def test_pairing_wrong_code_then_right_code_once(server):
    _, info, client, _ = server
    wrong = "000000" if info["code"] != "000000" else "111111"
    status, body = client.call("POST", "/v1/pair", {"code": wrong, "device_id": PHONE, "name": "Phone"})
    assert status == 403 and body["error"] == "wrong_code" and body["details"]["tries_left"] == serve.PAIRING_TRIES - 1
    paired = _pair(client, info)
    assert paired["device"]["device_id"] == PHONE and paired["server"]["device_id"] == info["device_id"]
    status, body = client.call("POST", "/v1/pair", {"code": info["code"], "device_id": "other", "name": "Other"})
    assert status == 403 and body["error"] == "pairing_closed"
    status, body = client.call("GET", "/v1/status")
    assert status == 200 and body["device"]["name"] == "Test phone"
    assert sync_errors("status", body) == []


def test_pair_rejects_a_bad_device_id(server):
    _, info, client, _ = server
    status, body = client.call("POST", "/v1/pair", {"code": info["code"], "device_id": "no spaces", "name": "Phone"})
    assert status == 400 and body["error"] == "invalid"
    status, _ = client.call("GET", "/v1/hello")
    assert status == 200  # and the code is still good
    _pair(client, info)


def test_tokens_are_stored_hashed(server, byobot_home):
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    with db(byobot_home) as s:
        stored = s.conn.execute("SELECT token_sha256 FROM peers").fetchone()[0]
    assert stored == hashlib.sha256(token.encode()).hexdigest() and token not in stored


def test_pull_the_plan(server, byobot_home):
    _, info, client, _ = server
    with db(byobot_home) as s:
        seed(s)
    _pair(client, info)
    status, body = client.call("GET", "/v1/plan?from=2026-11-22&to=2026-12-05")
    assert status == 200 and sync_errors("plan_window", body) == []
    assert len(body["planned_sessions"]) == 2 and body["from"] == "2026-11-22"
    status, body = client.call("GET", "/v1/plan?from=nope")
    assert status == 400


def test_push_events_round_trip(server, byobot_home):
    _, info, client, _ = server
    with db(byobot_home) as s:
        seed(s)
    _pair(client, info)
    sid, eid = uuid7(), uuid7()
    answers = {"kind": "planned_session", "id": LOWER}
    session = _event("session", sid, {"name": "Lower A", "modality": "self_reported", "links": {"answers": answers},
                                      "time": {"start": "2026-11-23T07:30:00-05:00"}})
    effort = _event("effort", eid, {"activity_id": "barbell_back_squat", "round": 2, "modality": "self_reported",
                                    "time": {"start": "2026-11-23T07:40:00-05:00"},
                                    "links": {"part_of": {"kind": "session", "id": sid},
                                              "answers": {**answers, "part": f"{LOWER}.0.0"}},
                                    "metrics": {"reps": {"value": 5}, "load": {"value": 225, "unit": "[lb_av]"}}})
    finished = _event("session", sid, {**session["payload"], "time": {"start": "2026-11-23T07:30:00-05:00",
                                                                      "end": "2026-11-23T08:20:00-05:00"}},
                      prev=[session["id"]])
    not_mine = _event("effort", uuid7(), effort["payload"], device_id="someone-else")
    invalid = _event("effort", uuid7(), {**effort["payload"], "metrics": {"reps": {"value": 5, "unit": "kg"}}})
    imported = _event("source_record", uuid7(), {"source": "whoop"})
    request = {"events": [session, effort, finished, not_mine, invalid, imported]}
    assert sync_errors("events_request", {"events": [session, effort, finished]}) == []
    status, body = client.call("POST", "/v1/events", request)
    assert status == 200 and sync_errors("events_response", body) == []
    assert [r["status"] for r in body["results"]] == ["accepted"] * 3 + ["rejected"] * 3
    assert "paired device" in body["results"][3]["reason"] and "a person writes" in body["results"][5]["reason"]

    # Retrying is harmless: the phone may not have seen the answer.
    status, body = client.call("POST", "/v1/events", {"events": [session, effort]})
    assert [r["status"] for r in body["results"]] == ["duplicate", "duplicate"] and body["duplicate"] == 2
    # A different event reusing an id is not a duplicate.
    status, body = client.call("POST", "/v1/events", {"events": [{**effort, "ts": "2026-11-23T09:00:00Z"}]})
    assert body["results"][0]["status"] == "rejected"

    with db(byobot_home) as s:
        row = s.rows("SELECT * FROM strength_sets WHERE id = ?", (eid,))[0]
        assert row["reps"] == 5 and abs(row["load_kg"] - 102.058) < 0.01 and row["location_id"] is None
        assert s.rows("SELECT end_ms FROM sessions WHERE id = ?", (sid,))[0]["end_ms"] is not None
        pva = s.rows("SELECT * FROM plan_vs_actual WHERE effort_id = ?", (eid,))[0]
        assert pva["planned_effort_id"] == f"{LOWER}.0.0" and pva["round"] == 2
        assert {r["device_id"] for r in s.rows("SELECT device_id FROM events WHERE record_id = ?", (sid,))} == {PHONE}

    # The effort now shows up in the plan the phone pulls.
    status, body = client.call("GET", "/v1/plan?from=2026-11-22&to=2026-12-05")
    assert eid in {e["id"] for e in body["efforts"]}

    # Undoing a set on the phone retracts it.
    gone = _event("effort", eid, {"reason": "Logged twice."}, op="retract", prev=[effort["id"]])
    status, body = client.call("POST", "/v1/events", {"events": [gone]})
    assert body["accepted"] == 1, body
    with db(byobot_home) as s:
        assert s.rows("SELECT * FROM efforts WHERE id = ?", (eid,)) == []


def test_a_phone_cannot_push_an_erased_event(server, byobot_home):
    _, info, client, _ = server
    _pair(client, info)
    eid = uuid7()
    blank = {**_event("effort", eid, {}), "redacted_by": uuid7()}
    status, body = client.call("POST", "/v1/events", {"events": [blank]})
    assert body["results"][0]["status"] == "rejected"


def test_push_rejects_bad_requests(server):
    _, info, client, _ = server
    _pair(client, info)
    assert client.call("POST", "/v1/events", {"nope": []})[0] == 400
    assert client.call("POST", "/v1/events", {"events": [1]})[1]["results"][0]["status"] == "rejected"
    assert client.call("GET", "/v1/nowhere")[0] == 404


def test_an_edit_on_the_phone_and_one_by_the_coach_show_the_conflict(server, byobot_home):
    _, info, client, _ = server
    with db(byobot_home) as s:
        seed(s)
    _pair(client, info)
    entry = next(e for e in client.call("GET", "/v1/plan?from=2026-11-22&to=2026-12-05")[1]["planned_sessions"]
                 if e["id"] == UPPER)
    with db(byobot_home) as s:  # the coach replans while the phone is offline
        commands.replan(s, AGENT, UPPER, lift("2026-11-26", "Upper A (moved)"))
    mine = _event("planned_session", UPPER, {**entry["payload"], "name": "Upper A (shorter)"}, prev=entry["prev"])
    assert client.call("POST", "/v1/events", {"events": [mine]})[1]["accepted"] == 1
    entry = next(e for e in client.call("GET", "/v1/plan?from=2026-11-22&to=2026-12-05")[1]["planned_sessions"]
                 if e["id"] == UPPER)
    assert entry["conflict"] is True and len(entry["prev"]) == 1
    edited = _event("planned_session", UPPER, {**entry["payload"], "notes": "short on time"}, prev=entry["prev"])
    assert client.call("POST", "/v1/events", {"events": [edited]})[1]["accepted"] == 1
    with db(byobot_home) as s:  # editing one version doesn't choose it
        [found] = s.conflicts("planned_session", UPPER)
        fixed = commands.resolve(s, ME, "planned_session", UPPER, edited["id"], [v["id"] for v in found["versions"]])
    entry = next(e for e in client.call("GET", "/v1/plan?from=2026-11-22&to=2026-12-05")[1]["planned_sessions"]
                 if e["id"] == UPPER)
    assert "conflict" not in entry and entry["payload"]["notes"] == "short on time" and entry["prev"] == [fixed["id"]]


def test_an_edit_pushed_before_its_original_still_wins(server, byobot_home):
    _, info, client, _ = server
    _pair(client, info)
    eid = uuid7()
    payload = {"activity_id": "barbell_back_squat", "modality": "self_reported",
               "time": {"start": "2026-11-23T07:35:00-05:00"}, "metrics": {"reps": {"value": 5}}}
    original = _event("effort", eid, payload)
    fix = _event("effort", eid, {**payload, "metrics": {"reps": {"value": 4}}}, prev=[original["id"]])
    assert client.call("POST", "/v1/events", {"events": [fix, original]})[1]["accepted"] == 2
    with db(byobot_home) as s:
        assert s.get("effort", eid)["payload"]["metrics"] == {"reps": {"value": 4}}
        assert not s.get("effort", eid)["conflict"]


def test_pull_events_after_a_cursor(server, byobot_home, monkeypatch):
    _, info, client, _ = server
    with db(byobot_home) as s:
        seed(s)
        total = s.rows("SELECT count(*) AS n FROM events")[0]["n"]
    _pair(client, info)
    status, body = client.call("GET", "/v1/events")
    assert status == 200 and sync_errors("pull_response", body) == []
    assert len(body["events"]) == total and body["more"] is False
    assert client.call("GET", f"/v1/events?after={body['next']}")[1] == {"events": [], "next": body["next"],
                                                                         "more": False}
    monkeypatch.setattr(serve, "MAX_PULL", 3)
    first = client.call("GET", "/v1/events?after=0")[1]
    second = client.call("GET", f"/v1/events?after={first['next']}")[1]
    assert first["more"] is True and len(first["events"]) == 3
    assert [e["id"] for e in first["events"] + second["events"]] == [e["id"] for e in body["events"][:6]]
    assert client.call("GET", "/v1/events?after=-1")[0] == 400


def test_push_samples(server, byobot_home):
    _, info, client, _ = server
    _pair(client, info)
    request = next(c for c in SYNC["cases"] if c["name"] == "sync/samples-request")["data"]
    weight = request["samples"][0]
    status, body = client.call("POST", "/v1/samples", {"samples": [weight]})
    assert status == 200 and body == {"written": 1, "by_source": {"apple_health": 1}}
    assert sync_errors("samples_response", body) == []
    assert client.call("POST", "/v1/samples", {"samples": [weight]})[1]["written"] == 0
    with db(byobot_home) as s:
        assert s.rows("SELECT code, value FROM observations") == [{"code": "body_mass", "value": 82.4}]
    gone = {"source": weight["source"], "source_id": weight["source_id"]}
    assert client.call("POST", "/v1/samples", {"deleted": [gone]})[1]["written"] == 1
    with db(byobot_home) as s:
        assert s.rows("SELECT code FROM observations") == []
    status, body = client.call("POST", "/v1/samples", {"samples": [{"source": "apple_health"}]})
    assert status == 400 and body["error"] == "invalid"
    assert client.call("POST", "/v1/samples", [weight])[0] == 400


def test_a_phone_writes_only_apple_health(server, byobot_home):
    """A paired device can't sign samples, or delete them, as another source."""
    _, info, client, _ = server
    _pair(client, info)
    weight = next(c for c in SYNC["cases"] if c["name"] == "sync/samples-request")["data"]["samples"][0]
    whoop = {**weight, "source": "whoop"}
    status, body = client.call("POST", "/v1/samples", {"samples": [whoop]})
    assert status == 400 and body["error"] == "invalid"
    from byobot.core import sources
    with db(byobot_home) as s:
        sources.ingest_samples(s, [whoop])
    status, body = client.call("POST", "/v1/samples", {"deleted": [{"source": "whoop", "source_id": weight["source_id"]}]})
    assert status == 400
    with db(byobot_home) as s:
        assert s.rows("SELECT source FROM source_records") == [{"source": "whoop"}]


def test_unpair_revokes_the_token_at_once(server, capsys):
    from byobot.cli import main

    _, info, client, _ = server
    _pair(client, info)
    assert client.call("GET", "/v1/status")[0] == 200
    assert main(["devices"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1 and listed["items"][0]["device_id"] == PHONE
    assert main(["unpair", PHONE]) == 0
    assert json.loads(capsys.readouterr().out)["device"]["revoked_at"]
    assert client.call("GET", "/v1/status")[0] == 401
    assert main(["unpair", "nobody"]) == 1


def test_pairing_opens_by_default_only_until_a_device_is_paired(byobot_home):
    srv, info = serve.make_server("127.0.0.1", 0)
    srv.server_close()
    assert "code" in info
    with db(byobot_home) as s:
        s.pair(PHONE, "Phone", "0" * 64)
    srv, info = serve.make_server("127.0.0.1", 0)
    srv.server_close()
    assert "code" not in info
    srv, info = serve.make_server("127.0.0.1", 0, pair=True)
    srv.server_close()
    assert "code" in info and "link" in info


def test_serve_command_reports_a_busy_port(capsys):
    from byobot.cli import main

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert main(["serve", "--host", "127.0.0.1", "--port", str(port)]) == 1
    assert "could not listen" in capsys.readouterr().err


def test_pushed_events_cannot_name_another_actor(server, byobot_home):
    _, info, client, _ = server
    _pair(client, info)
    mine = _event("activity", "plank", {"name": "Plank"})
    agent = _event("activity", "curl", {"name": "Curl"}, actor={"kind": "agent", "id": "example-agent"})
    other = _event("activity", "run", {"name": "Run"}, actor={"kind": "human", "id": "example-laptop"})
    status, body = client.call("POST", "/v1/events", {"events": [mine, agent, other]})
    assert status == 200 and sync_errors("events_response", body) == []
    assert [r["status"] for r in body["results"]] == ["accepted", "rejected", "rejected"]
    assert "user" in body["results"][1]["reason"] and "user" in body["results"][2]["reason"]
    assert client.call("POST", "/v1/events", {"events": [mine]})[1]["results"][0]["status"] == "duplicate"
    with db(byobot_home) as s:
        assert s.rows("SELECT actor_kind, actor_id, actor_model FROM events") == [
            {"actor_kind": "human", "actor_id": "me", "actor_model": None}]


def test_a_nested_body_is_rejected(server):
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    nested = b"[" * 2000 + b"]" * 2000
    request = _post_headers("/v1/events", f"Authorization: Bearer {token}", f"Content-Length: {len(nested)}") + nested
    data = _raw_exchange(info["port"], info["fingerprint"], request)
    assert _status_line(data) == 400 and _json_body(data)["error"] == "invalid"
    assert client.call("GET", "/v1/status")[0] == 200


def _raw_exchange(port, fp, request: bytes, timeout: float = 3) -> bytes:
    """One HTTPS request written as bytes, so a test can stop before the body and see if the server waits."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        tls = ctx.wrap_socket(sock, server_hostname="127.0.0.1")
        assert hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() == fp
        tls.sendall(request)
        tls.settimeout(timeout)
        chunks = []
        try:
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            chunks.append(b"\n<timeout>")
        return b"".join(chunks)
    finally:
        sock.close()


def _status_line(data: bytes) -> int:
    assert not data.endswith(b"<timeout>"), data[:300]
    line = data.split(b"\r\n", 1)[0]
    return int(line.split()[1])


def _json_body(data: bytes) -> dict:
    return json.loads(data.split(b"\r\n\r\n", 1)[1])


def _post_headers(path: str, *headers: str) -> bytes:
    lines = [f"POST {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close", *headers, "", ""]
    return "\r\n".join(lines).encode()


def test_unauthenticated_post_is_rejected_before_the_body_is_read(server):
    """A missing or wrong token is a 401 even when the body never arrives, and never a 100 Continue."""
    _, info, client, _ = server
    for path in ("/v1/events", "/v1/samples"):
        for auth in ("", "Authorization: Bearer not-a-token"):
            headers = ["Content-Length: 1000000", "Expect: 100-continue"]
            if auth:
                headers.append(auth)
            data = _raw_exchange(info["port"], info["fingerprint"], _post_headers(path, *headers))
            assert not data.startswith(b"HTTP/1.1 100"), path
            assert _status_line(data) == 401 and b"Connection: close" in data
            assert _json_body(data)["error"] == "unauthorized"
    # A negative length must not be read either (read(-1) would block until the client closes).
    data = _raw_exchange(info["port"], info["fingerprint"],
                         _post_headers("/v1/events", "Content-Length: -1", "Authorization: Bearer not-a-token"))
    assert _status_line(data) == 401
    status, body = client.call("GET", "/v1/hello")
    assert status == 200 and body["pairing_open"] is True


def _tls_client(port, fp):
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    tls = ctx.wrap_socket(sock, server_hostname="127.0.0.1")
    assert hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() == fp
    return tls


def _read_headers(tls, timeout: float = 3) -> bytes:
    tls.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = tls.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            break
    return data


def test_expect_100_continue_is_sent_only_after_auth(server):
    """A client waiting on 100 gets 401 with no Continue, or 100 and then 200 once the token and length pass."""
    _, info, client, _ = server
    refused = _raw_exchange(info["port"], info["fingerprint"], _post_headers(
        "/v1/events", "Content-Length: 13", "Expect: 100-continue", "Authorization: Bearer not-a-token"))
    assert refused.startswith(b"HTTP/1.1 401")
    assert b"HTTP/1.1 100" not in refused
    assert _json_body(refused)["error"] == "unauthorized"

    token = _pair(client, info)["token"]
    too_big = _raw_exchange(info["port"], info["fingerprint"], _post_headers(
        "/v1/events", f"Authorization: Bearer {token}", f"Content-Length: {serve.MAX_BODY + 1}", "Expect: 100-continue"))
    assert too_big.startswith(b"HTTP/1.1 413") and b"HTTP/1.1 100" not in too_big

    body = b'{"events":[]}'
    headers = (
        "POST /v1/events HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Expect: 100-continue\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    tls = _tls_client(info["port"], info["fingerprint"])
    try:
        tls.sendall(headers)
        prelude = _read_headers(tls)
        assert prelude.startswith(b"HTTP/1.1 100"), prelude
        assert b"HTTP/1.1 200" not in prelude
        tls.sendall(body)
        rest = b""
        tls.settimeout(3)
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            rest += chunk
    finally:
        tls.close()
    assert rest.startswith(b"HTTP/1.1 200"), rest[:200]
    assert json.loads(rest.split(b"\r\n\r\n", 1)[1])["results"] == []


def test_a_refused_post_does_not_desync_the_connection(server):
    """Bytes already sent with a 401 are not parsed as the next request."""
    _, info, _, _ = server
    smuggled = b"GET /v1/hello HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    request = (
        "POST /v1/events HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Content-Length: {len(smuggled)}\r\n"
        "Authorization: Bearer not-a-token\r\n"
        "Connection: keep-alive\r\n"
        "\r\n"
    ).encode() + smuggled
    data = _raw_exchange(info["port"], info["fingerprint"], request)
    assert data.startswith(b"HTTP/1.1 401")
    assert b"HTTP/1.1 200" not in data
    assert b"Connection: close" in data
    assert _json_body(data)["error"] == "unauthorized"


def test_writes_are_not_under_the_query_deadline(server, monkeypatch):
    _, info, client, _ = server
    _pair(client, info)
    monkeypatch.setattr(serve, "QUERY_TIMEOUT_S", 0)
    monkeypatch.setattr(serve, "QUERY_PROGRESS_INTERVAL", 1)
    event = _event("activity", "plank", {"name": "Plank"})
    status, body = client.call("POST", "/v1/events", {"events": [event]})
    assert status == 200 and body["accepted"] == 1
    status, body = client.call("POST", "/v1/samples", {"samples": []})
    assert status == 200 and body["written"] == 0
    assert client.call("GET", "/v1/status")[0] == 503


def test_content_length_is_validated_before_the_body_is_read(server):
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    auth = f"Authorization: Bearer {token}"
    cases = [
        (["Authorization: Bearer " + token], 411, "length_required"),  # missing
        ([auth, "Content-Length: -1"], 400, "invalid"),
        ([auth, "Content-Length: nope"], 400, "invalid"),
        ([auth, "Content-Length: +12"], 400, "invalid"),
        ([auth, f"Content-Length: {serve.MAX_BODY + 1}"], 413, "too_large"),
        ([auth, "Content-Length: 2", "Content-Length: 2"], 400, "invalid"),
        ([auth, "Content-Length: 5", "Transfer-Encoding: chunked"], 400, "invalid"),
        ([auth, "Content-Length: 0"], 400, "invalid"),  # present and empty: not "missing", and not a hang
    ]
    for headers, status, code in cases:
        data = _raw_exchange(info["port"], info["fingerprint"], _post_headers("/v1/events", *headers))
        assert _status_line(data) == status, headers
        body = _json_body(data)
        assert body["error"] == code, headers
        assert sync_errors("error", body) == []
        if "Content-Length: 0" not in headers:
            assert b"Connection: close" in data  # the body was not read, so the connection must not be reused
    # The token still works, and a normal body is accepted.
    assert client.call("POST", "/v1/events", {"events": []})[0] == 200


def test_pairing_rejects_a_bad_length_without_spending_a_guess(server):
    _, info, client, _ = server
    huge = _post_headers("/v1/pair", f"Content-Length: {serve.MAX_PAIR_BODY + 1}")
    negative = _post_headers("/v1/pair", "Content-Length: -1")
    assert _status_line(_raw_exchange(info["port"], info["fingerprint"], huge)) == 413
    assert _status_line(_raw_exchange(info["port"], info["fingerprint"], negative)) == 400
    wrong = "000000" if info["code"] != "000000" else "111111"
    status, body = client.call("POST", "/v1/pair", {"code": wrong, "device_id": PHONE, "name": "Example iPhone"})
    assert status == 403 and body["details"]["tries_left"] == serve.PAIRING_TRIES - 1
    _pair(client, info)


def test_a_stalled_client_is_disconnected(server, monkeypatch):
    monkeypatch.setattr(serve, "SOCKET_TIMEOUT_S", 1)
    _, info, client, _ = server
    started = time.monotonic()
    data = _raw_exchange(info["port"], info["fingerprint"],
                         b"POST /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 10\r\n", timeout=4)
    assert time.monotonic() - started < 5
    assert not data.endswith(b"<timeout>")
    assert b"HTTP/1.1 200" not in data
    assert client.call("GET", "/v1/hello")[0] == 200


def test_a_slow_body_is_stopped(server, monkeypatch):
    monkeypatch.setattr(serve, "REQUEST_DEADLINE_S", 1)
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    head = _post_headers("/v1/events", f"Authorization: Bearer {token}", "Content-Length: 100000")
    # Headers plus one body byte, then nothing. The declared length is in range, so only the deadline can stop it.
    data = _raw_exchange(info["port"], info["fingerprint"], head + b"{", timeout=5)
    assert _status_line(data) == 408
    assert _json_body(data)["error"] == "timeout"
    assert client.call("GET", "/v1/status")[0] == 200


def test_plan_and_status_give_up_on_a_long_query(server, monkeypatch):
    _, info, client, _ = server
    _pair(client, info)
    monkeypatch.setattr(serve, "QUERY_TIMEOUT_S", 0)
    monkeypatch.setattr(serve, "QUERY_PROGRESS_INTERVAL", 1)
    for path in ("/v1/plan", "/v1/status"):
        status, body = client.call("GET", path)
        assert status == 503 and body["error"] == "timeout", path
        assert sync_errors("error", body) == []
    assert client.call("GET", "/v1/hello")[0] == 200


def test_default_profile_keeps_8765_and_profiles_get_their_own_port(byobot_home, monkeypatch):
    assert serve.choose_port("127.0.0.1") == serve.DEFAULT_PORT
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    port = serve.choose_port("127.0.0.1")
    assert port > serve.DEFAULT_PORT + 1
    serve.serve_dir().mkdir(parents=True)
    (serve.serve_dir() / "port").write_text("9001\n")
    assert serve.choose_port("127.0.0.1") == 9001


def test_profile_shows_in_the_server_name(monkeypatch):
    monkeypatch.setattr(serve.socket, "gethostname", lambda: "example-laptop")
    assert serve.server_name() == "example-laptop"
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    assert serve.server_name() == "example-laptop (alex)"
