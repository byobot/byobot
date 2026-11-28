"""``byobot serve``: the LAN sync endpoint the app pairs with, over real HTTPS."""

import base64
import hashlib
import http.client
import json
import socket
import sqlite3
import ssl
import time
from contextlib import contextmanager
from datetime import datetime

import pytest

from byobot import link, remote, serve, web
from byobot.core import query as query_module
from byobot.core import commands, hlc, relay
from byobot.core.contract import Contract, sync_errors
from byobot.core.store import Store
from byobot.util import uuid7
from conftest import newer_contract

from helpers import ME, PHONE, fixture, serving, while_another_process_writes

SYNC = fixture("sync.json")
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
            link.parse_pairing_link(item["link"])
        return
    parsed = link.parse_pairing_link(item["link"])
    assert parsed.pop("v") == link.VERSION and parsed == item["expect"]


def test_pairing_link_round_trips():
    pin = hashlib.sha256(b"x").hexdigest()
    made = link.pairing_link(hosts=["host.local", "192.0.2.2"], port=8765, code="012345", pin=pin)
    assert link.parse_pairing_link(made) == {"v": 1, "hosts": ["host.local", "192.0.2.2"], "port": 8765,
                                             "code": "012345", "pin": pin}


def test_the_pin_is_the_public_key_s_so_a_renewed_certificate_keeps_it(byobot_home):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    cert_path, key_path, pin = serve.ensure_certificate("dev_1")
    old = x509.load_pem_x509_certificate(cert_path.read_bytes())
    key = serialization.load_pem_private_key(key_path.read_bytes(), None)
    renewed = (x509.CertificateBuilder().subject_name(old.subject).issuer_name(old.issuer).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(old.not_valid_before_utc)
               .not_valid_after(old.not_valid_after_utc).sign(key, hashes.SHA256()))
    der = renewed.public_bytes(serialization.Encoding.DER)
    assert der != old.public_bytes(serialization.Encoding.DER) and serve.fingerprint(der) == pin
    assert serve.fingerprint(base64.b64decode(SYNC["pin"]["certificate"])) == SYNC["pin"]["pin"]


# -- pairing codes -------------------------------------------------------------

def test_pairing_code_is_single_use():
    p = web.Pairing()
    assert len(p.code) == 6 and p.code.isdigit() and p.open
    assert p.check(p.code[:3] + "-" + p.code[3:])
    assert not p.open and not p.check(p.code)


def test_pairing_closes_after_too_many_wrong_codes():
    p = web.Pairing()
    wrong = "000000" if p.code != "000000" else "111111"
    for _ in range(web.PAIRING_TRIES):
        assert not p.check(wrong)
    assert not p.open and not p.check(p.code)


def test_pairing_expires(monkeypatch):
    p = web.Pairing()
    monkeypatch.setattr(web.time, "monotonic", lambda: p.expires_at + 1)
    assert not p.open and not p.check(p.code)


# -- what the phone pulls ------------------------------------------------------

LOWER, UPPER, LATER, PLAN, DONE = (f"0193a0b0-0000-7000-8000-0000000001{i:02d}" for i in range(5))


def write(store, *records, actor=AGENT):
    return commands.log(store, actor, [{"kind": k, "id": i, "payload": p} for k, i, p in records])


def lift(day, name, location="example_gym"):
    return {"scheduled_on": day, "name": name, "location_id": location, "links": {"part_of": {"kind": "plan", "id": PLAN}},
            "blocks": [{"rounds": 3, "efforts": [{"activity_id": "x.barbell_back_squat",
                                                  "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}}]}]}


def seed(store):
    write(store,
          ("activity", "x.barbell_back_squat", {"name": "Barbell back squat", "targets": {"main": ["quads"]}}),
          ("location", "example_gym", {"name": "Example gym"}),
          ("setting", "equipment", {"value": ["barbell", "rack"]}),
          ("plan", PLAN, {"name": "Block 1", "starts_on": "2026-11-23"}),
          ("planned_session", LOWER, lift("2026-11-23", "Lower A")),
          ("planned_session", UPPER, lift("2026-11-25", "Upper A")),
          ("planned_session", LATER, lift("2027-01-10", "Much later")))
    answers = {"kind": "planned_session", "id": LOWER}
    write(store, ("session", DONE, {"name": "Lower A", "location_id": "example_gym", "links": {"answers": answers},
                                    "time": {"start": "2026-11-23T07:30:00-05:00"}}),
          ("effort", None, {"activity_id": "x.barbell_back_squat", "round": 1, "time": {"start": "2026-11-23T07:35:00-05:00"},
                            "links": {"part_of": {"kind": "session", "id": DONE},
                                      "answers": {**answers, "part": f"{LOWER}.0.0"}},
                            "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}}), actor=ME)


def test_plan_window_is_contract_shaped(store):
    seed(store)
    window = relay.plan_window(store, "2026-11-22", "2026-12-05")
    assert sync_errors("plan_window", window) == []
    assert [s["id"] for s in window["planned_sessions"]] == [LOWER, UPPER]
    for entry in window["planned_sessions"]:
        assert entry["payload"] == store.get("planned_session", entry["id"])["payload"]  # exactly as authored
        assert entry["prev"] == store.record("planned_session", entry["id"])["tips"]
    assert [p["id"] for p in window["plans"]] == [PLAN]
    assert [a["id"] for a in window["activities"]] == ["x.barbell_back_squat"]
    assert [s["id"] for s in window["settings"]] == ["equipment"]
    assert [s["id"] for s in window["sessions"]] == [DONE]
    [effort] = window["efforts"]
    assert effort["payload"]["round"] == 1 and effort["payload"]["metrics"]["load"] == {"value": 100, "unit": "kg"}


def test_a_phone_cant_change_a_record_newer_than_it_knows(tmp_path):
    hub = Store(tmp_path / "hub.db")
    payload = {"activity_id": "x.barbell_back_squat", "metrics": {"reps": {"value": 5}},
               "time": {"start": "2026-11-23T07:40:00-05:00"}}
    [plain] = commands.log(hub, ME, [{"kind": "effort", "payload": payload}])
    hub.use(newer_contract(Contract.shipped(), 2))
    [fast] = commands.log(hub, ME, [{"kind": "effort", "payload": payload}])
    entries = {e["id"]: e["kind_version"] for e in relay._entries(hub, "effort", [plain["record"]["id"],
                                                                               fast["record"]["id"]])}
    assert entries == {plain["record"]["id"]: 1, fast["record"]["id"]: 2}
    edits = [_event("effort", e["record"]["id"], {**payload, "notes": "felt good"}, prev=[e["id"]])
             for e in (plain, fast)]
    results = relay.accept_events(hub, PHONE, edits, ME)
    assert [r["status"] for r in results] == ["accepted", "rejected"] and "update the app" in results[1]["reason"]
    assert relay.accept_events(hub, PHONE, edits[1:], ME, {"effort": 2})[0]["status"] == "accepted"


def test_plan_window_sends_the_library_activities_it_names(byobot_home, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "activities").mkdir(parents=True)
    (lib / "vocabulary.json").write_text("{}")
    dip = {"name": "Dip", "targets": {"main": ["triceps"]}, "resistance_curve": {"stretch": 3, "middle": 2, "contract": 1}}
    (lib / "activities" / "dip.json").write_text(json.dumps(dip))
    (lib / "activities" / "push_up.json").write_text(json.dumps({"name": "Push-up"}))
    monkeypatch.setenv("BYOBOT_LIBRARY", str(lib))
    with Store(byobot_home / "lib.db") as s:
        assert relay.plan_window(s, "2026-11-22", "2026-11-28")["activities"] == []  # the app ships the library
        write(s, ("location", "example_gym", {"name": "Example gym"}),
              ("plan", PLAN, {"name": "Block 1", "starts_on": "2026-11-23"}),
              ("planned_session", LOWER, {**lift("2026-11-23", "Lower A"), "blocks": [{"efforts": [{"activity_id": "dip"}]}]}))
        window = relay.plan_window(s, "2026-11-22", "2026-11-28")
    assert window["activities"] == [{"id": "dip", "payload": dip, "prev": [], "kind_version": 1, "library": True}]
    assert sync_errors("plan_window", window) == []


def test_plan_window_follows_replanning_and_undo(store):
    seed(store)
    replanned = commands.replan(store, AGENT, LOWER, lift("2026-11-23", "Lower A (deload)"))

    def names():
        window = relay.plan_window(store, "2026-11-01", "2026-12-31")
        return {s["id"]: s["payload"]["name"] for s in window["planned_sessions"]}

    assert names()[LOWER] == "Lower A (deload)"
    commands.undo(store, ME, replanned["id"])
    assert names()[LOWER] == "Lower A"


def test_plan_window_shows_a_conflict(store):
    seed(store)
    tips, payload = store.record("planned_session", UPPER)["tips"], store.get("planned_session", UPPER)["payload"]
    for name in ("Upper A (phone)", "Upper A (coach)"):
        store.write(ME, [{"kind": "planned_session", "id": UPPER, "op": "put", "prev": tips,
                          "payload": {**payload, "name": name}}])
    window = relay.plan_window(store, "2026-11-22", "2026-12-05")
    assert sync_errors("plan_window", window) == []
    [entry] = [s for s in window["planned_sessions"] if s["id"] == UPPER]
    assert entry["conflict"] is True and entry["prev"] == [store.record("planned_session", UPPER)["current"]]
    assert {v["event_id"] for v in entry["versions"]} == set(store.record("planned_session", UPPER)["heads"])
    assert sorted(v["payload"]["name"] for v in entry["versions"]) == ["Upper A (coach)", "Upper A (phone)"]
    assert all(v["actor"] == ME and v["ts"] for v in entry["versions"])
    store.write(ME, [{"kind": "planned_session", "id": UPPER, "op": "put", "prev": entry["prev"],
                      "payload": {**entry["payload"], "notes": "from the phone"}}])
    assert store.record("planned_session", UPPER)["conflict"]  # a phone edit keeps the conflict


def test_a_phone_resolves_a_conflict_by_naming_every_version(store):
    seed(store)
    tips, payload = store.record("planned_session", UPPER)["tips"], store.get("planned_session", UPPER)["payload"]
    for name in ("Upper A (phone)", "Upper A (coach)"):
        store.write(ME, [{"kind": "planned_session", "id": UPPER, "op": "put", "prev": tips,
                          "payload": {**payload, "name": name}}])
    [entry] = [s for s in relay.plan_window(store, "2026-11-22", "2026-12-05")["planned_sessions"] if s["id"] == UPPER]
    kept = next(v for v in entry["versions"] if v["payload"]["name"] == "Upper A (phone)")
    resolve = _event("planned_session", UPPER, kept["payload"], prev=[v["event_id"] for v in entry["versions"]])
    assert relay.accept_events(store, PHONE, [resolve], ME)[0]["status"] == "accepted"
    assert not store.record("planned_session", UPPER)["conflict"]
    assert store.get("planned_session", UPPER)["payload"]["name"] == "Upper A (phone)"
    [entry] = [s for s in relay.plan_window(store, "2026-11-22", "2026-12-05")["planned_sessions"] if s["id"] == UPPER]
    assert "versions" not in entry and "conflict" not in entry


def test_plan_window_outside_the_dates_still_brings_the_last_time(store):
    seed(store)
    window = relay.plan_window(store, "2030-01-01", "2030-01-07")
    assert window["planned_sessions"] == [] and window["since"] == "2029-11-02"
    assert [s["id"] for s in window["sessions"]] == [DONE] and len(window["efforts"]) == 1


def squats(store, visits, activity="x.barbell_back_squat"):
    for i, (day, location) in enumerate(visits):
        sid = f"0193a0b0-0000-7000-8000-0000000005{i:02d}"
        write(store, ("session", sid, {"name": "Squats", "location_id": location,
                                       "time": {"start": f"{day}T08:00:00Z"}}),
              *[("effort", f"0193a0b0-0000-7000-8000-0000000006{i}{n}", {
                  "activity_id": activity, "round": n, "time": {"start": f"{day}T08:0{n}:00Z"},
                  "links": {"part_of": {"kind": "session", "id": sid}},
                  "metrics": {"load": {"value": 90 + i, "unit": "kg"}}}) for n in (1, 2)], actor=ME)


def test_plan_window_brings_the_last_two_times_at_each_location(store):
    seed(store)
    squats(store, [("2026-07-10", "home"), ("2026-07-12", "home"), ("2026-07-14", "home"), ("2026-07-15", "hotel_gym")])
    window = relay.plan_window(store, "2026-11-25", "2026-11-30")
    ours = [e["id"][-2:] for e in window["efforts"] if e["id"].startswith("0193a0b0-0000-7000-8000-0000000006")]
    assert ours == ["11", "12", "21", "22", "31", "32"] and len(window["efforts"]) == 7  # and the seeded gym visit
    assert [p["id"] for p in window["planned_sessions"]] == [UPPER]


def test_plan_window_brings_the_last_time_of_an_exercise_the_plan_doesnt_have(store):
    seed(store)
    write(store, ("activity", "x.cable_fly", {"name": "Cable fly"}))
    squats(store, [("2026-06-01", "home"), ("2026-06-03", "example_gym")], activity="x.cable_fly")
    window = relay.plan_window(store, "2026-11-25", "2026-11-30")
    flies = [e for e in window["efforts"] if e["payload"]["activity_id"] == "x.cable_fly"]
    assert len(flies) == 4  # each gym's last time, for a swap or an exercise added on the day
    sessions = {s["id"] for s in window["sessions"]}
    assert {e["payload"]["links"]["part_of"]["id"] for e in flies} <= sessions


def test_plan_window_carries_the_history_before_it(store):
    seed(store)
    squats(store, [("2026-09-20", "home"), ("2026-09-30", "home"), ("2026-10-10", "home")])
    write(store, ("effort", None, {"activity_id": "x.barbell_back_squat", "time": {"start": "2026-10-11T08:00:00Z"},
                                   "metrics": {"reps": {"value": 3}}}), actor=ME)
    window = relay.plan_window(store, "2026-11-25", "2026-11-30")
    assert window["since"] == "2026-09-26"
    days = sorted(s["payload"]["time"]["start"][:10] for s in window["sessions"])
    assert days == ["2026-09-30", "2026-10-10", "2026-11-23"]  # the one before since is no longer a last time
    assert any("links" not in e["payload"] for e in window["efforts"])  # and a set outside a session
    assert sync_errors("plan_window", window) == []


# -- over HTTPS ----------------------------------------------------------------

class Client:
    """A minimal client that pins the server's certificate, as the app does."""

    def __init__(self, port, fp, token=None):
        self.port, self.fp, self.token = port, fp, token

    def raw(self, method, path, data: bytes | None = None, token=None, timeout: float = 15):
        """(status, body) or raises ConnectionError when the server closes without answering."""
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # trust comes from the pinned fingerprint below
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=timeout)
        try:
            conn.connect()
            served = serve.fingerprint(conn.sock.getpeercert(binary_form=True))
            assert served == self.fp, "certificate does not match the pinned fingerprint"
            headers = {"Content-Type": "application/json"}
            tok = token if token is not None else self.token
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            conn.request(method, path, body=data, headers=headers)
            try:
                resp = conn.getresponse()
            except (http.client.RemoteDisconnected, ConnectionResetError, ssl.SSLError) as exc:
                raise ConnectionError(f"no answer: {exc!r}") from None
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, body
        finally:
            conn.close()

    def call(self, method, path, body=None, token=None, timeout: float = 15):
        return self.raw(method, path, None if body is None else json.dumps(body).encode(), token, timeout)


@contextmanager
def running(**kw):
    """A running hub with pairing open, and a client pinned to its certificate."""
    logs = []
    kw = {"pair": True, "log": logs.append, **kw}
    srv, info = serve.make_server("127.0.0.1", 0, **kw)
    with serving(srv):
        yield srv, info, Client(info["port"], info["fingerprint"]), logs


@pytest.fixture
def server(byobot_home):
    with running() as h:
        yield h


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



def location(slug, name):
    return _event("location", slug, {"name": name})


def no_answer(fn) -> bool:
    try:
        fn()
    except (ConnectionError, socket.timeout, TimeoutError):
        return True
    return False

def db(byobot_home):
    return Store(byobot_home / "byobot.db")


def test_the_shared_exchanges_answer_as_the_fixture_says(server):
    _, info, client, _ = server
    wrong = "000000" if info["code"] != "000000" else "111111"
    token = None
    for x in SYNC["exchanges"]:
        body = json.loads(json.dumps(x["body"]).replace("{code}", info["code"]).replace("{wrong}", wrong))
        auth = {"none": "", "wrong": "not-a-token", "paired": token}[x["auth"]]
        status, answer = client.call(x["method"], x["path"], body, token=auth)
        assert status == x["status"], (x["name"], answer)
        if "error" in x:
            assert answer["error"] == x["error"] and sync_errors("error", answer) == [], x["name"]
        else:
            assert sync_errors(x["def"], answer) == [], x["name"]
        if x.get("def") == "pair_response":
            token = answer["token"]


def test_certificate_is_made_once_and_kept(server, byobot_home):
    _, info, _, _ = server
    cert = byobot_home / "serve" / "cert.pem"
    assert cert.is_file() and (byobot_home / "serve" / "key.pem").is_file()
    assert serve.fingerprint(cert.read_bytes()) == info["fingerprint"]
    with db(byobot_home) as s:
        _, _, again = serve.ensure_certificate(s.device_id)
    assert again == info["fingerprint"]
    assert info["link"].startswith("byobot://pair?v=1&") and link.parse_pairing_link(info["link"])["pin"] == again


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
    assert sync_errors("hello", body) == [] and body["kind_versions"]["effort"] == 1 and body["capabilities"] == []
    assert client.call("GET", "/v0/hello")[0] == 404  # an app on the old protocol is told nothing it would misread


def test_everything_else_needs_a_token(server):
    _, _, client, _ = server
    for method, path in (("GET", "/v1/status"), ("GET", "/v1/plan"), ("POST", "/v1/events"),
                         ("POST", "/v1/samples")):
        status, body = client.call(method, path, {"events": [], "samples": []} if method == "POST" else None,
                                   token="not-a-token")
        assert status == 401 and body["error"] == "unauthorized", path
        assert sync_errors("error", body) == []


def test_pairing_wrong_code_then_right_code_once(server):
    _, info, client, _ = server
    wrong = "000000" if info["code"] != "000000" else "111111"
    status, body = client.call("POST", "/v1/pair", {"code": wrong, "device_id": PHONE, "name": "Phone"})
    assert status == 403 and body["error"] == "wrong_code" and body["details"]["tries_left"] == web.PAIRING_TRIES - 1
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


def test_pair_takes_the_app_s_capabilities_and_ignores_names_it_doesn_t_know(server):
    _, info, client, _ = server
    asked = {"code": info["code"], "device_id": PHONE, "name": "Example iPhone"}
    status, body = client.call("POST", "/v1/pair", {**asked, "capabilities": "handover"})
    assert status == 400 and body["error"] == "invalid"
    status, _ = client.call("POST", "/v1/pair", {**asked, "capabilities": ["handover", "something_newer"]})
    assert status == 200 and sync_errors("pair_request", {**asked, "capabilities": ["handover"]}) == []
    _pair(client, info)


def test_tokens_are_stored_hashed(server, byobot_home):
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    with db(byobot_home) as s:
        stored = s.conn.execute("SELECT token_sha256 FROM devices").fetchone()[0]
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
    session = _event("session", sid, {"name": "Lower A", "links": {"answers": answers},
                                      "time": {"start": "2026-11-23T07:30:00-05:00"}})
    effort = _event("effort", eid, {"activity_id": "x.barbell_back_squat", "round": 2,
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
    assert client.call("POST", "/v1/events", {"events": [], "kind_versions": {"effort": 0}})[0] == 400
    assert client.call("POST", "/v1/events", {"events": [], "kind_versions": {"effort": 2}})[0] == 200
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
    payload = {"activity_id": "x.barbell_back_squat",
               "time": {"start": "2026-11-23T07:35:00-05:00"}, "metrics": {"reps": {"value": 5}}}
    original = _event("effort", eid, payload)
    fix = _event("effort", eid, {**payload, "metrics": {"reps": {"value": 4}}}, prev=[original["id"]])
    assert client.call("POST", "/v1/events", {"events": [fix, original]})[1]["accepted"] == 2
    with db(byobot_home) as s:
        assert s.get("effort", eid)["payload"]["metrics"] == {"reps": {"value": 4}}
        assert not s.get("effort", eid)["conflict"]


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
    assert main(["device", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1 and listed["items"][0]["id"] == PHONE and listed["items"][0]["route"] == "hub"
    assert main(["device", "remove", PHONE]) == 0
    assert json.loads(capsys.readouterr().out)["removed"]["route"] == "hub"
    assert client.call("GET", "/v1/status")[0] == 401
    assert main(["device", "list"]) == 0 and json.loads(capsys.readouterr().out)["count"] == 0
    assert main(["device", "remove", PHONE]) == 1 and main(["device", "remove", "nobody"]) == 1


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
        assert serve.fingerprint(tls.getpeercert(binary_form=True)) == fp
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
    assert serve.fingerprint(tls.getpeercert(binary_form=True)) == fp
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
        "/v1/events", f"Authorization: Bearer {token}", f"Content-Length: {web.MAX_BODY + 1}", "Expect: 100-continue"))
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
    monkeypatch.setattr(query_module, "PROGRESS_INTERVAL", 1)
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
        ([auth, f"Content-Length: {web.MAX_BODY + 1}"], 413, "too_large"),
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
    assert status == 403 and body["details"]["tries_left"] == web.PAIRING_TRIES - 1
    _pair(client, info)


def test_a_stalled_client_is_disconnected(server, monkeypatch):
    monkeypatch.setattr(web, "SOCKET_TIMEOUT_S", 1)
    _, info, client, _ = server
    started = time.monotonic()
    data = _raw_exchange(info["port"], info["fingerprint"],
                         b"POST /v1/events HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 10\r\n", timeout=4)
    assert time.monotonic() - started < 5
    assert not data.endswith(b"<timeout>")
    assert b"HTTP/1.1 200" not in data
    assert client.call("GET", "/v1/hello")[0] == 200


def test_a_slow_body_is_stopped(server, monkeypatch):
    monkeypatch.setattr(web, "REQUEST_DEADLINE_S", 1)
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
    monkeypatch.setattr(query_module, "PROGRESS_INTERVAL", 1)
    for path in ("/v1/plan", "/v1/status"):
        status, body = client.call("GET", path)
        assert status == 503 and body["error"] == "timeout", path
        assert sync_errors("error", body) == []
    assert client.call("GET", "/v1/hello")[0] == 200


def test_default_profile_keeps_8765_and_profiles_get_their_own_port(byobot_home, monkeypatch):
    assert serve.choose_port() == serve.DEFAULT_PORT
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    port = serve.choose_port()
    assert port > serve.DEFAULT_PORT + 1
    serve.serve_dir().mkdir(parents=True)
    (serve.serve_dir() / "port").write_text("9001\n")
    assert serve.choose_port() == 9001


def test_choosing_a_port_probes_loopback_only(byobot_home, monkeypatch):
    bound = []

    class Probe(serve.socket.socket):
        def bind(self, address):
            bound.append(address[0])
            return super().bind((address[0], 0))

    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    monkeypatch.setattr(serve.util.socket, "socket", Probe)
    serve.choose_port()
    assert bound and set(bound) == {"127.0.0.1"}


def test_profile_shows_in_the_server_name(monkeypatch):
    monkeypatch.setattr(serve.socket, "gethostname", lambda: "example-laptop")
    assert serve.server_name() == "example-laptop"
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    assert serve.server_name() == "example-laptop (alex)"


def test_a_new_location_under_an_erased_slug_is_not_reported_as_kept(store):
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    commands.erase(store, ME, "location", "example_gym")

    # On the phone, which last pulled before the erase, you add "Example gym" again: same slug, prev [].
    added = _event("location", "example_gym", {"name": "Example gym"})
    [result] = relay.accept_events(store, PHONE, [added])

    kept = store.get("location", "example_gym") is not None
    assert kept or result["status"] == "rejected", (
        f"the hub answered {result['status']!r} but kept nothing; the phone drops the location at its next pull")


@pytest.fixture
def deadlined(byobot_home, monkeypatch):
    monkeypatch.setattr(web, "REQUEST_DEADLINE_S", 5)  # 30 s in production; scaled down, with room for slow CI
    with running() as h:
        yield h


def _connect(port):
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5))


def _response(sock) -> tuple[int, dict, bytes]:
    """One HTTP/1.1 response read off a kept-alive connection."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError(f"closed after {data!r}")
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    headers = {k.lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:])}
    length = int(headers.get("content-length", 0))
    while len(rest) < length:
        rest += sock.recv(4096)
    return int(lines[0].split()[1]), headers, rest[:length]


def _post(path, token, body: bytes) -> bytes:
    return (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body


def test_each_request_on_a_kept_alive_connection_gets_its_own_deadline(deadlined):
    _, info, client, _ = deadlined
    token = _pair(client, info)["token"]
    sock = _connect(info["port"])
    statuses = []
    # A first sync sends a year of Apple Health readings, a thousand at a time, one request after another.
    for _ in range(30):
        sock.sendall(_post("/v1/samples", token, json.dumps({"samples": []}).encode()))
        try:
            status, _, _ = _response(sock)
        except ConnectionError:
            status = "closed"
        statuses.append(status)
        if status != 200:
            break
        time.sleep(0.2)  # each request is quick; together they outlast one deadline
    sock.close()
    assert statuses == [200] * 30, statuses


def test_a_refusal_after_an_earlier_post_on_the_connection_does_not_desync_it(deadlined):
    _, info, client, _ = deadlined
    token = _pair(client, info)["token"]
    sock = _connect(info["port"])
    sock.sendall(_post("/v1/samples", token, json.dumps({"samples": []}).encode()))
    assert _response(sock)[0] == 200
    # Refused before its body is read: the body must be dropped or the connection closed, never parsed.
    inner = b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n\r\n"
    sock.sendall(_post("/v1/events", "not-a-token", inner))
    status, headers, _ = _response(sock)
    assert status == 401
    assert headers.get("connection", "").lower() == "close", "the unread body stays on a kept-alive connection"
    sock.settimeout(2)
    try:
        extra = _response(sock)
    except (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError, OSError):
        extra = None
    assert extra is None, f"the body was answered as a request of its own: {extra}"


BASE = "https://example-laptop.example.ts.net"


@pytest.fixture
def remote_server(byobot_home):
    Store(byobot_home / "byobot.db").close()
    with serving(remote.make_server(BASE, "127.0.0.1", 0, pair=True)) as server:
        yield server


def _exchange(sock, raw: bytes) -> bytes:
    sock.sendall(raw)
    out = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out += chunk
    except (TimeoutError, socket.timeout, ConnectionError, ssl.SSLError):
        pass
    return out


def test_a_pairing_code_in_other_digits_gets_an_answer_and_spends_a_try(server):
    srv, info, client, _ = server
    # "١٢٣٤٥٦" is 123456 in Arabic-Indic digits: not ASCII, so secrets.compare_digest raises TypeError.
    body = json.dumps({"code": "١٢٣٤٥٦", "device_id": "example-phone",
                       "name": "Example iPhone"}).encode()
    sock = _tls_client(info["port"], info["fingerprint"])
    reply = _exchange(sock, b"POST /v1/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                            b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sock.close()
    assert reply.startswith(b"HTTP/1.1 403"), f"no answer at all: {reply[:80]!r}"
    assert srv.pairing.tries_left == web.PAIRING_TRIES - 1


def test_an_approval_code_in_other_digits_gets_a_page(remote_server):
    from test_remote import CALLBACK, FORM, pkce, request

    resp, raw = request(remote_server, "POST", "/oauth/register", {"client_name": "Example Assistant",
                                                                   "redirect_uris": [CALLBACK],
                                                                   "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz"}
    try:
        resp, _ = request(remote_server, "POST", "/oauth/authorize", {**params, "approval_code": "¹²"}, FORM)
        status = resp.status
    except (http.client.HTTPException, ConnectionError):
        status = None
    assert status == 403, "the approval page dropped the connection instead of answering"


def test_the_log_never_carries_terminal_control_codes_from_a_stranger(server):
    _, info, _, logs = server
    sock = _tls_client(info["port"], info["fingerprint"])
    # ESC ] 0 ; ... BEL sets the terminal's title; other sequences can rewrite what `byobot serve` showed.
    _exchange(sock, b"GET /v1/\x1b]0;pwned\x07\x1b[2J HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    sock.close()
    assert logs, "nothing was logged"
    assert not any("\x1b" in line or "\x07" in line for line in logs), logs


def test_registration_with_a_malformed_grant_types_gets_an_oauth_error(remote_server):
    from test_remote import CALLBACK, request

    try:
        resp, raw = request(remote_server, "POST", "/oauth/register",
                            {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "grant_types": 5})
        status = resp.status
    except (http.client.HTTPException, ConnectionError):
        status = None
    assert status == 400, "an unauthenticated registration dropped the connection instead of answering"


def test_a_client_that_never_sends_redirect_uri_can_finish_sign_in(remote_server):
    """RFC 6749 4.1.3 (kept by OAuth 2.1): redirect_uri is required at the token endpoint only if it was in the
    authorization request. byobot lets a client with one registered address leave it out of the request, then
    insists on it at the token endpoint."""
    from urllib.parse import parse_qs, urlencode, urlsplit

    from test_remote import CALLBACK, FORM, pkce, request

    resp, raw = request(remote_server, "POST", "/oauth/register", {"client_name": "Example Assistant",
                                                                   "redirect_uris": [CALLBACK],
                                                                   "token_endpoint_auth_method": "none"})
    client = json.loads(raw)
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "xyz"}  # no redirect_uri
    resp, _ = request(remote_server, "GET", "/oauth/authorize?" + urlencode(params))
    assert resp.status == 200
    resp, _ = request(remote_server, "POST", "/oauth/authorize",
                      {**params, "approval_code": remote_server.pairing.code}, FORM)
    assert resp.status == 302
    code = parse_qs(urlsplit(resp.getheader("Location")).query)["code"][0]
    resp, raw = request(remote_server, "POST", "/oauth/token", {"grant_type": "authorization_code", "code": code,
                                                               "client_id": client["client_id"],
                                                               "code_verifier": verifier}, FORM)
    assert resp.status == 200, raw


def test_a_get_with_a_body_does_not_desync_the_next_request_on_the_connection(remote_server):
    # Behind a tunnel, one upstream connection carries requests from different callers, so leftover body bytes
    # would become the start of someone else's request.
    smuggled = b"GET /api/v1/openapi.json HTTP/1.1\r\nHost: x\r\n\r\n"
    first = (b"GET /.well-known/oauth-authorization-server HTTP/1.1\r\nHost: x\r\nContent-Length: "
             + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled)
    second = b"GET /.well-known/oauth-protected-resource HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    sock = socket.create_connection(("127.0.0.1", remote_server.server_address[1]), timeout=5)
    reply = _exchange(sock, first + second)
    sock.close()
    answers = reply.count(b"HTTP/1.1 ")
    assert b'"openapi"' not in reply, "the body of the first request was answered as a request of its own"
    assert answers <= 2


def test_a_restore_while_serving_is_what_phones_are_told(server, byobot_home, tmp_path):
    _, info, client, _ = server
    with Store(tmp_path / "old-laptop.db") as old:
        commands.log(old, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
        backup, original = list(old.export()), old.store_id
    with Store(byobot_home / "byobot.db") as s:  # `byobot restore backup.jsonl` in another terminal
        s.restore(backup)
        assert s.store_id == original

    status, hello = client.call("GET", "/v1/hello")
    assert status == 200
    paired = _pair(client, info)
    assert hello["server"]["store_id"] == original
    assert paired["server"]["store_id"] == original, (
        "the phone records another store, so it never sends again what the old laptop held after the backup")


def test_the_plan_names_the_restored_store(server, byobot_home, tmp_path):
    _, info, client, _ = server
    paired = _pair(client, info)
    with Store(tmp_path / "old-laptop.db") as old:
        backup, original = list(old.export()), old.store_id
    with Store(byobot_home / "byobot.db") as s:
        s.restore(backup)
    client.token = paired["token"]
    status, plan = client.call("GET", "/v1/plan")
    assert status == 200 and plan["store_id"] == original


def _accounted(window: dict, pushed: list[dict], held: set[str]) -> list[str]:
    """Ids the phone's settle would drop although the hub neither holds nor lists them as refused."""
    marker = window["received"].get(PHONE)
    refused = {r["id"] for r in window["rejected"]}
    return [e["id"] for e in pushed if marker and e["hlc"] <= marker and e["id"] not in held
            and e["id"] not in refused]


def test_a_refusal_before_a_failure_mid_push_is_still_reported(server, byobot_home, monkeypatch):
    srv, info, client, _ = server
    _pair(client, info)
    refused = location("example_gym", "Example gym")
    refused["actor"] = {"kind": "agent", "id": "example-coach"}  # refused: a phone pushes only as its person
    kept = location("example_park", "Example park")
    cut = location("example_pool", "Example pool")

    real = Store.accept

    def accept(self, e):
        if isinstance(e, dict) and e.get("id") == cut["id"]:
            # What SQLite raises when another process held the write lock past the 5 s busy timeout.
            raise sqlite3.OperationalError("database is locked")
        return real(self, e)

    monkeypatch.setattr(Store, "accept", accept)
    pushed = [refused, kept, cut]
    no_answer(lambda: client.call("POST", "/v1/events", {"events": pushed, "kind_versions": {}}))
    monkeypatch.setattr(Store, "accept", real)

    status, window = client.call("GET", "/v1/plan")
    assert status == 200, window
    with Store(byobot_home / "byobot.db") as s:
        held = {e["id"] for e in pushed if s.event(e["id"])}
    assert kept["id"] in held  # committed before the failure, so `received` already names it
    assert _accounted(window, pushed, held) == [], (
        "the refused event is at or below `received` but not in `rejected`, so the phone's settle drops it")


def test_a_push_that_fails_part_way_still_answers(server, monkeypatch):
    """The phone should learn the outcome of the events that went in, not get a dropped connection."""
    srv, info, client, _ = server
    _pair(client, info)
    first, second = location("example_park", "Example park"), location("example_pool", "Example pool")
    real = Store.accept

    def accept(self, e):
        if isinstance(e, dict) and e.get("id") == second["id"]:
            raise sqlite3.OperationalError("database is locked")
        return real(self, e)

    monkeypatch.setattr(Store, "accept", accept)
    try:
        status, body = client.call("POST", "/v1/events", {"events": [first, second], "kind_versions": {}})
    except ConnectionError as exc:
        pytest.fail(f"the push got no HTTP answer: {exc}")
    assert status in (200, 503), body


def test_a_plan_read_while_another_process_writes_is_answered(server, byobot_home):
    srv, info, client, _ = server
    _pair(client, info)
    try:
        status, body = while_another_process_writes(byobot_home / "byobot.db",
                                                     lambda: client.call("GET", "/v1/plan", timeout=20))
    except ConnectionError as exc:
        pytest.fail(f"the plan read got no HTTP answer while another process wrote: {exc}")
    assert status == 200, body


def test_a_push_while_another_process_writes_gets_an_answer(server, byobot_home):
    srv, info, client, _ = server
    _pair(client, info)
    events = [location("example_park", "Example park")]
    try:
        status, body = while_another_process_writes(
            byobot_home / "byobot.db",
            lambda: client.call("POST", "/v1/events", {"events": events, "kind_versions": {}}, timeout=20))
    except ConnectionError as exc:
        pytest.fail(f"the push got no HTTP answer while another process wrote: {exc}")
    assert status in (200, 503), body


def test_a_plan_is_one_snapshot_even_when_an_erase_lands_while_it_is_read(store, byobot_home, monkeypatch):
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    real = relay._device_state

    def erase_meanwhile(s, device_id):
        with Store(byobot_home / "byobot.db") as other:  # the CLI, on its own connection
            commands.erase(other, ME, "location", "example_gym")
        return real(s, device_id)

    monkeypatch.setattr(relay, "_device_state", erase_meanwhile)
    window = relay.plan_window(store, "2026-11-01", "2026-11-14", PHONE)
    live = {e["id"] for e in window["locations"]}
    erased = {e["id"] for e in window["erased"] if e["kind"] == "location"}
    assert not live & erased, "the window shows example_gym as live, with its payload, and as erased"


def test_a_push_after_an_erase_compacts_the_file_at_most_once(store, byobot_home):
    commands.log(store, ME, [{"kind": "location", "id": "example_gym", "payload": {"name": "Example gym"}}])
    reader = sqlite3.connect(byobot_home / "byobot.db", isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM events").fetchone()  # another process mid-read, e.g. a remote tool call
    commands.erase(store, ME, "location", "example_gym")
    assert store.compact_pending()

    vacuums = []
    store.conn.set_trace_callback(lambda sql: vacuums.append(sql) if sql.strip().upper() == "VACUUM" else None)
    from byobot.core import hlc
    from byobot.util import uuid7
    events = []
    for i in range(20):
        events.append({"id": uuid7(), "schema_version": 1, "record": {"kind": "location", "id": f"example_place_{i}"},
                       "op": "put", "kind_version": 1, "hlc": hlc.format((1795700000000 + i * 1000, 0)),
                       "ts": "2026-11-23T08:00:00-05:00", "device_id": PHONE, "actor": ME, "prev": [],
                       "payload": {"name": f"Example place {i}"}})
    results = relay.accept_events(store, PHONE, events)
    reader.execute("COMMIT")
    reader.close()
    assert all(r["status"] == "accepted" for r in results)
    assert len(vacuums) <= 1, f"one push of {len(events)} events ran VACUUM {len(vacuums)} times"


class _Busy(socket.socket):
    """Ports below 8780 are taken by other programs on this computer."""

    def bind(self, address):
        if address[1] < remote.DEFAULT_PORT:
            raise OSError(98, "Address already in use")
        return super().bind((address[0], 0))


def test_a_named_profile_hub_never_takes_the_remote_port(monkeypatch):
    monkeypatch.setenv("BYOBOT_PROFILE", "example")
    monkeypatch.setattr(serve.socket, "socket", _Busy)
    port = serve.choose_port()
    remote_ports = range(remote.DEFAULT_PORT, remote.DEFAULT_PORT + 100)
    assert port not in remote_ports, f"serve chose {port}, which `byobot remote` uses or will choose"


def _sample(value: str) -> bytes:
    return ('{"samples": [{"source": "apple_health", "source_id": "example-sample-1", '
            '"type": "HKQuantityTypeIdentifierBodyMass", "start": "2026-11-01T08:00:00Z", '
            f'"end": "2026-11-01T08:00:00Z", "value": {value}, "unit": "kg"}}]}}').encode()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e400"])
def test_a_sample_with_a_number_json_cannot_hold_is_refused_with_an_answer(server, value):
    srv, info, client, _ = server
    _pair(client, info)
    try:
        status, body = client.raw("POST", "/v1/samples", _sample(value))
    except ConnectionError as exc:
        pytest.fail(f"no HTTP answer for a sample valued {value}: {exc}")
    assert status == 400, body


def test_an_event_body_with_a_huge_integer_gets_an_answer(server):
    srv, info, client, _ = server
    _pair(client, info)
    body = b'{"events": [], "kind_versions": {}, "x": ' + b"9" * 5000 + b"}"
    try:
        status, answer = client.raw("POST", "/v1/events", body)
    except ConnectionError as exc:
        pytest.fail(f"no HTTP answer: {exc}")
    assert status == 400, answer


def test_an_unauthenticated_registration_with_a_huge_integer_gets_an_answer(remote_server):
    body = (b'{"redirect_uris": ["https://client.example/cb"], "token_endpoint_auth_method": "none", "x": '
            + b"9" * 5000 + b"}")
    conn = http.client.HTTPConnection("127.0.0.1", remote_server.server_address[1], timeout=10)
    try:
        conn.request("POST", "/oauth/register", body=body, headers={"Content-Type": "application/json"})
        try:
            resp = conn.getresponse()
        except (http.client.RemoteDisconnected, ConnectionResetError) as exc:
            pytest.fail(f"a stranger's registration got no HTTP answer: {exc!r}")
        assert resp.status == 400
    finally:
        conn.close()


def test_a_pairing_whose_token_could_not_be_stored_can_be_tried_again(server, monkeypatch):
    srv, info, client, _ = server
    real = Store.pair
    calls = []

    def flaky(self, *args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")  # the CLI held the write lock past 5 s
        return real(self, *args)

    monkeypatch.setattr(Store, "pair", flaky)
    request = {"code": info["code"], "device_id": "0193a0b0-0000-7000-8000-00000000ab32", "name": "Example iPhone"}
    try:
        client.call("POST", "/v1/pair", request)
    except ConnectionError:
        pass  # no answer today; a 503 would be right too
    status, body = client.call("POST", "/v1/pair", request)
    assert status == 200, body


def test_hello_names_the_computer_only_while_pairing_is_open(byobot_home):
    with running(pair=False) as (srv, _, client, _):
        srv.name = "Example Person's laptop"
        status, body = client.call("GET", "/v1/hello")
    assert status == 200 and body["pairing_open"] is False
    assert body["server"]["name"] == "byobot"


def test_a_removed_phone_cannot_pair_back_with_the_code_it_spent(server, byobot_home):
    _, info, client, _ = server
    _pair(client, info)
    with db(byobot_home) as s:
        s.unpair(PHONE)
    status, body = client.call("POST", "/v1/pair", {"code": info["code"], "device_id": PHONE, "name": "Test phone"},
                               token="")
    assert status == 403 and body["error"] == "pairing_closed"


def test_an_event_whose_id_is_not_text_is_refused(server):
    _, info, client, _ = server
    _pair(client, info)
    bad = {**_event("location", "example_gym", {"name": "Example gym"}), "id": ["not", "text"]}
    status, body = client.call("POST", "/v1/events", {"events": [bad]})
    assert status == 200 and body["results"][0]["reason"] == "not an event"
