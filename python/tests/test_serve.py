"""``byobot serve``: the LAN sync endpoint the app pairs with, over real HTTPS."""

import hashlib
import http.client
import json
import socket
import ssl
import threading
import time

import pytest
from conftest import CASES, FIXTURES, case

from byobot import contract, serve
from byobot.store import Store

SYNC = json.loads((FIXTURES.parent / "sync.json").read_text(encoding="utf-8"))
PHONE = "0193a0b0-0000-7000-8000-00000000ab01"


# -- the shared protocol fixtures ---------------------------------------------

@pytest.mark.parametrize("item", SYNC["cases"], ids=[c["name"] for c in SYNC["cases"]])
def test_sync_fixture(item):
    errors = contract.validate_sync(item["def"], item["data"])
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
    assert serve.parse_pairing_link(link) == {"v": 0, "hosts": ["host.local", "192.0.2.2"], "port": 8765,
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

def _seed(store):
    for item in CASES:
        if item["schema"] == "event" and item["valid"] and item["data"]["type"] != "event.undone":
            store.accept(item["data"])


def test_plan_window_is_contract_shaped(store):
    _seed(store)
    window = serve.plan_window(store, "2026-11-22", "2026-12-05")
    assert contract.validate_sync("plan_window", window) == []
    planned = {c["data"]["payload"]["id"]: c["data"]["payload"] for c in CASES
               if c["valid"] and c["schema"] == "event" and c["data"]["type"] == "session.planned"}
    assert {s["id"] for s in window["sessions"]} == set(planned)
    for s in window["sessions"]:
        assert s == planned[s["id"]]  # exactly as authored
    assert [s["scheduled_on"] for s in window["sessions"]] == sorted(s["scheduled_on"] for s in window["sessions"])
    assert "plan-2026-w48" in {p["id"] for p in window["plans"]}
    assert {a["id"] for a in window["activities"]} >= {"barbell_back_squat"}
    logged = {e["id"]: e for e in window["efforts"]}
    assert logged["eff-1"]["round"] == 2 and logged["eff-1"]["metrics"]["load"] == {"value": 100, "unit": "kg"}


def test_plan_window_sends_the_library(byobot_home, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "activities").mkdir(parents=True)
    (lib / "vocabulary.json").write_text("{}")
    dip = {"id": "dip", "name": "Dip", "targets": {"main": ["triceps"]}, "resistance_curve": {"stretch": 3, "middle": 2, "contract": 1}}
    (lib / "activities" / "dip.json").write_text(json.dumps(dip))
    monkeypatch.setenv("BYOBOT_LIBRARY", str(lib))
    with Store(byobot_home / "lib.db") as s:
        window = serve.plan_window(s, "2026-11-22", "2026-11-28")
    assert window["activities"] == [dip] and contract.validate_sync("plan_window", window) == []


def test_plan_window_follows_replanning_and_undo(store):
    _seed(store)
    first = case("event/session-planned-lift")
    again = {**first, "id": "0193a0b0-0000-7000-8000-0000000000f1",
             "payload": {**first["payload"], "name": "Lower A (deload)"}}
    store.accept(again)
    names = {s["id"]: s["name"] for s in serve.plan_window(store, "2026-11-01", "2026-12-31")["sessions"]}
    assert names[first["payload"]["id"]] == "Lower A (deload)"
    store.append("event.undone", {"event_id": again["id"]}, {"kind": "human", "id": "me"})
    names = {s["id"]: s["name"] for s in serve.plan_window(store, "2026-11-01", "2026-12-31")["sessions"]}
    assert names[first["payload"]["id"]] == first["payload"]["name"]


def test_plan_window_outside_the_dates_is_empty(store):
    _seed(store)
    window = serve.plan_window(store, "2030-01-01", "2030-01-07")
    assert window["sessions"] == [] and window["efforts"] == []


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
    status, body = client.call("POST", "/v0/pair", {"code": info["code"], "device_id": device_id, "name": "Test phone"})
    assert status == 200, body
    assert contract.validate_sync("pair_response", body) == []
    client.token = body["token"]
    return body


def _event(type_, payload, device_id=PHONE, event_id=None):
    from byobot.util import now, uuid7

    return {"id": event_id or uuid7(), "schema_version": 0, "type": type_, "ts": now(), "device_id": device_id,
            "actor": {"kind": "human", "id": "me"}, "payload": payload}


def test_certificate_is_made_once_and_kept(server, byobot_home):
    _, info, _, _ = server
    cert = byobot_home / "serve" / "cert.pem"
    assert cert.is_file() and (byobot_home / "serve" / "key.pem").is_file()
    assert serve.fingerprint(cert.read_bytes()) == info["fingerprint"]
    with Store(byobot_home / "byobot.db") as s:
        _, _, again = serve.ensure_certificate(s.device_id)
    assert again == info["fingerprint"]
    assert info["link"].startswith("byobot://pair?v=0&") and serve.parse_pairing_link(info["link"])["fp"] == again


def test_plain_http_is_refused(server):
    _, info, _, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
    with pytest.raises((http.client.HTTPException, ConnectionError, socket.timeout, OSError)):
        conn.request("GET", "/v0/hello")
        conn.getresponse().read()
    conn.close()


def test_hello_needs_no_token(server):
    _, info, client, _ = server
    status, body = client.call("GET", "/v0/hello")
    assert status == 200 and body["pairing_open"] is True and body["server"]["device_id"] == info["device_id"]
    assert contract.validate_sync("hello", body) == []


def test_everything_else_needs_a_token(server):
    _, _, client, _ = server
    for method, path in (("GET", "/v0/status"), ("GET", "/v0/plan"), ("POST", "/v0/events"), ("POST", "/v0/telemetry")):
        status, body = client.call(method, path, {"events": [], "samples": []} if method == "POST" else None,
                                   token="not-a-token")
        assert status == 401 and body["error"] == "unauthorized", path
        assert contract.validate_sync("error", body) == []


def test_pairing_wrong_code_then_right_code_once(server):
    _, info, client, _ = server
    wrong = "000000" if info["code"] != "000000" else "111111"
    status, body = client.call("POST", "/v0/pair", {"code": wrong, "device_id": PHONE, "name": "Phone"})
    assert status == 403 and body["error"] == "wrong_code" and body["details"]["tries_left"] == serve.PAIRING_TRIES - 1
    paired = _pair(client, info)
    assert paired["device"]["device_id"] == PHONE and paired["server"]["device_id"] == info["device_id"]
    status, body = client.call("POST", "/v0/pair", {"code": info["code"], "device_id": "other", "name": "Other"})
    assert status == 403 and body["error"] == "pairing_closed"
    status, body = client.call("GET", "/v0/status")
    assert status == 200 and body["device"]["name"] == "Test phone"
    assert contract.validate_sync("status", body) == []


def test_pair_rejects_a_bad_device_id(server):
    _, info, client, _ = server
    status, body = client.call("POST", "/v0/pair", {"code": info["code"], "device_id": "no spaces", "name": "Phone"})
    assert status == 400 and body["error"] == "invalid"
    status, _ = client.call("GET", "/v0/hello")
    assert status == 200  # and the code is still good
    _pair(client, info)


def test_tokens_are_stored_hashed(server, byobot_home):
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    with Store(byobot_home / "byobot.db") as s:
        stored = s.conn.execute("SELECT token_sha256 FROM sync_peers").fetchone()[0]
    assert stored == hashlib.sha256(token.encode()).hexdigest() and token not in stored


def test_pull_the_plan(server, byobot_home):
    srv, info, client, _ = server
    with Store(byobot_home / "byobot.db") as s:
        _seed(s)
    _pair(client, info)
    status, body = client.call("GET", "/v0/plan?from=2026-11-22&to=2026-12-05")
    assert status == 200 and contract.validate_sync("plan_window", body) == []
    assert len(body["sessions"]) == 6 and body["from"] == "2026-11-22"
    status, body = client.call("GET", "/v0/plan?from=nope")
    assert status == 400


def test_push_events_round_trip(server, byobot_home):
    _, info, client, _ = server
    with Store(byobot_home / "byobot.db") as s:
        _seed(s)
    _pair(client, info)
    session = _event("session.performed", {"id": "phone-sess-1", "planned_session_id": "sess-lower-a",
                                            "started_at": "2026-11-23T07:30:00-05:00"})
    effort = _event("effort.performed", {"id": "phone-eff-1", "session_id": "phone-sess-1", "activity_id": "barbell_back_squat",
                                         "performed_at": "2026-11-23T07:35:00-05:00", "planned_effort_id": "e-squat",
                                         "round": 1, "metrics": {"reps": {"value": 5}, "load": {"value": 225, "unit": "lb"}}})
    finished = _event("session.performed", {"id": "phone-sess-1", "planned_session_id": "sess-lower-a",
                                             "started_at": "2026-11-23T07:30:00-05:00",
                                             "ended_at": "2026-11-23T08:20:00-05:00"})
    not_mine = _event("effort.performed", {"id": "x", "activity_id": "plank", "performed_at": "2026-11-23T08:00:00Z"},
                      device_id="someone-else")
    invalid = _event("effort.performed", {"id": "y", "activity_id": "plank", "performed_at": "2026-11-23T08:00:00Z",
                                          "metrics": {"rep": {"value": 5}}})
    request = {"events": [session, effort, finished, not_mine, invalid]}
    assert contract.validate_sync("events_request", {"events": [session, effort, finished]}) == []
    status, body = client.call("POST", "/v0/events", request)
    assert status == 200 and contract.validate_sync("events_response", body) == []
    assert [r["status"] for r in body["results"]] == ["accepted", "accepted", "accepted", "rejected", "rejected"]
    assert body["results"][3]["errors"][0]["path"] == "$.device_id"
    assert any("rep" in e["message"] or "rep" in e["path"] for e in body["results"][4]["errors"])

    # Retrying is harmless: the phone may not have seen the answer.
    status, body = client.call("POST", "/v0/events", {"events": [session, effort]})
    assert [r["status"] for r in body["results"]] == ["duplicate", "duplicate"] and body["duplicate"] == 2
    # A different event reusing an id is not a duplicate.
    status, body = client.call("POST", "/v0/events", {"events": [{**effort, "ts": "2026-11-23T09:00:00Z"}]})
    assert body["results"][0]["status"] == "rejected"

    with Store(byobot_home / "byobot.db") as s:
        row = s.rows("SELECT * FROM strength_sets WHERE effort_id = 'phone-eff-1'")[0]
        assert row["reps"] == 5 and abs(row["load_kg"] - 102.058) < 0.01
        sess = s.rows("SELECT * FROM performed_sessions WHERE id = 'phone-sess-1'")[0]
        assert sess["device_id"] == PHONE and sess["ended_at"] == "2026-11-23T08:20:00-05:00"
        assert s.rows("SELECT count(*) AS n FROM performed_efforts WHERE session_id = 'phone-sess-1'")[0]["n"] == 1
        pva = s.rows("SELECT * FROM plan_vs_actual WHERE performed_effort_id = 'phone-eff-1'")[0]
        assert pva["planned_effort_id"] == "e-squat" and pva["round"] == 1

    # The effort now shows up in the plan the phone pulls.
    status, body = client.call("GET", "/v0/plan?from=2026-11-22&to=2026-12-05")
    assert "phone-eff-1" in {e["id"] for e in body["efforts"]}

    # Undo from the phone.
    status, body = client.call("POST", "/v0/events", {"events": [_event("event.undone", {"event_id": effort["id"]})]})
    assert body["accepted"] == 1
    with Store(byobot_home / "byobot.db") as s:
        assert s.rows("SELECT * FROM performed_efforts WHERE id = 'phone-eff-1'") == []


def test_push_rejects_bad_requests(server):
    _, info, client, _ = server
    _pair(client, info)
    assert client.call("POST", "/v0/events", {"nope": []})[0] == 400
    assert client.call("POST", "/v0/events", {"events": [1]})[1]["results"][0]["status"] == "rejected"
    assert client.call("GET", "/v0/nowhere")[0] == 404


def test_push_telemetry(server, byobot_home):
    _, info, client, _ = server
    _pair(client, info)
    good = SYNC["cases"][[c["name"] for c in SYNC["cases"]].index("sync/telemetry-request")]["data"]["samples"][0]
    bad = {**good, "source_id": "bad", "unit": "lb"}
    status, body = client.call("POST", "/v0/telemetry", {"samples": [good, bad]})
    assert status == 200 and body["inserted"] == 1 and body["rejected"][0]["index"] == 1
    assert contract.validate_sync("telemetry_response", body) == []
    status, body = client.call("POST", "/v0/telemetry", {"samples": [good]})
    assert body["unchanged"] == 1
    with Store(byobot_home / "byobot.db") as s:
        row = s.rows("SELECT * FROM telemetry_samples")[0]
        assert row["source"] == "apple-health" and row["value"] == 82.4


def test_unpair_revokes_the_token_at_once(server, capsys):
    from byobot.cli import main

    _, info, client, _ = server
    _pair(client, info)
    assert client.call("GET", "/v0/status")[0] == 200
    assert main(["devices"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1 and listed["items"][0]["device_id"] == PHONE
    assert main(["unpair", PHONE]) == 0
    assert json.loads(capsys.readouterr().out)["device"]["revoked_at"]
    assert client.call("GET", "/v0/status")[0] == 401
    assert main(["unpair", "nobody"]) == 1


def test_pairing_opens_by_default_only_until_a_device_is_paired(byobot_home):
    srv, info = serve.make_server("127.0.0.1", 0)
    srv.server_close()
    assert "code" in info
    with Store(byobot_home / "byobot.db") as s:
        s.pair_peer(PHONE, "Phone", "0" * 64)
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


def test_out_of_order_push_from_the_phone(server, byobot_home):
    """Ordering rule: the latest event time wins, whichever device's event arrives last."""
    _, info, client, _ = server
    _pair(client, info)
    with Store(byobot_home / "byobot.db") as s:
        laptop = s.device_id
        s.accept(_event("activity.defined", {"id": "plank", "name": "Plank (laptop, 12:00)"}, device_id=laptop)
                 | {"ts": "2026-11-21T12:00:00Z"})
    # The phone logged a rename at 11:00 offline and syncs it late: the laptop's newer edit stands.
    old = _event("activity.defined", {"id": "plank", "name": "Plank (phone, 11:00)"}) | {"ts": "2026-11-21T11:00:00Z"}
    # The phone finished a session at 13:00 but its outbox pushes the end before the start.
    end = _event("session.performed", {"id": "s1", "started_at": "2026-11-21T12:30:00Z",
                                        "ended_at": "2026-11-21T13:00:00Z"}) | {"ts": "2026-11-21T13:00:00Z"}
    start = _event("session.performed", {"id": "s1", "started_at": "2026-11-21T12:30:00Z"}) | {"ts": "2026-11-21T12:30:00Z"}
    status, body = client.call("POST", "/v0/events", {"events": [old, end, start]})
    assert body["accepted"] == 3
    with Store(byobot_home / "byobot.db") as s:
        assert s.rows("SELECT name FROM activities WHERE id = 'plank'")[0]["name"] == "Plank (laptop, 12:00)"
        assert s.rows("SELECT ended_at FROM performed_sessions WHERE id = 's1'")[0]["ended_at"] == "2026-11-21T13:00:00Z"
        # Arrival order is kept as the audit trail.
        seqs = [r["id"] for r in s.rows("SELECT id FROM events ORDER BY seq")]
        assert seqs[-3:] == [old["id"], end["id"], start["id"]]
    # A newer edit from the phone wins.
    new = _event("activity.defined", {"id": "plank", "name": "Plank (phone, 14:00)"}) | {"ts": "2026-11-21T14:00:00Z"}
    client.call("POST", "/v0/events", {"events": [new]})
    with Store(byobot_home / "byobot.db") as s:
        assert s.rows("SELECT name FROM activities WHERE id = 'plank'")[0]["name"] == "Plank (phone, 14:00)"


def test_a_correction_pushed_before_its_original_still_wins(server, byobot_home):
    _, info, client, _ = server
    with Store(byobot_home / "byobot.db") as s:
        _seed(s)
    _pair(client, info)
    effort = {"id": "phone-eff-2", "activity_id": "barbell_back_squat", "performed_at": "2026-11-23T07:35:00-05:00",
              "planned_effort_id": "e-squat", "round": 1, "metrics": {"reps": {"value": 5}}}
    original = _event("effort.performed", effort) | {"ts": "2026-11-23T12:35:05Z"}
    fix = _event("effort.performed", {**effort, "metrics": {"reps": {"value": 4}}}) | {"ts": "2026-11-23T12:40:00Z"}
    status, body = client.call("POST", "/v0/events", {"events": [fix, original]})
    assert body["accepted"] == 2
    status, body = client.call("GET", "/v0/plan?from=2026-11-22&to=2026-12-05")
    logged = [e for e in body["efforts"] if e["id"] == "phone-eff-2"]
    assert logged == [{**effort, "metrics": {"reps": {"value": 4}}}]


def test_plan_window_has_locations_and_where_each_set_happened(store):
    store.accept(case("event/location-defined"))
    store.accept(case("event/session-planned-at-location"))
    session = json.loads(json.dumps(case("event/session-performed-at-location")))
    session["payload"]["efforts"][0]["planned_effort_id"] = "0193a0b0-0000-7000-8000-000000000412"
    store.accept(session)
    window = serve.plan_window(store, "2026-11-22", "2026-11-30")
    assert contract.validate_sync("plan_window", window) == []
    assert window["locations"] == [{"id": "home", "name": "Home gym", "notes": "Example bar, iron plates"}]
    assert window["sessions"][0]["location_id"] == "home"
    assert [e["location_id"] for e in window["efforts"]] == ["example-gym"]  # taken from its session


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
    for path in ("/v0/events", "/v0/telemetry"):
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
                         _post_headers("/v0/events", "Content-Length: -1", "Authorization: Bearer not-a-token"))
    assert _status_line(data) == 401
    status, body = client.call("GET", "/v0/hello")
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
        "/v0/events", "Content-Length: 13", "Expect: 100-continue", "Authorization: Bearer not-a-token"))
    assert refused.startswith(b"HTTP/1.1 401")
    assert b"HTTP/1.1 100" not in refused
    assert _json_body(refused)["error"] == "unauthorized"

    token = _pair(client, info)["token"]
    too_big = _raw_exchange(info["port"], info["fingerprint"], _post_headers(
        "/v0/events", f"Authorization: Bearer {token}", f"Content-Length: {serve.MAX_BODY + 1}", "Expect: 100-continue"))
    assert too_big.startswith(b"HTTP/1.1 413") and b"HTTP/1.1 100" not in too_big

    body = b'{"events":[]}'
    headers = (
        "POST /v0/events HTTP/1.1\r\n"
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
    smuggled = b"GET /v0/hello HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    request = (
        "POST /v0/events HTTP/1.1\r\n"
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
    event = _event("activity.defined", {"id": "plank", "name": "Plank"})
    status, body = client.call("POST", "/v0/events", {"events": [event]})
    assert status == 200 and body["accepted"] == 1
    status, body = client.call("POST", "/v0/telemetry", {"samples": []})
    assert status == 200 and body["inserted"] == 0
    assert client.call("GET", "/v0/status")[0] == 503


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
        data = _raw_exchange(info["port"], info["fingerprint"], _post_headers("/v0/events", *headers))
        assert _status_line(data) == status, headers
        body = _json_body(data)
        assert body["error"] == code, headers
        assert contract.validate_sync("error", body) == []
        if "Content-Length: 0" not in headers:
            assert b"Connection: close" in data  # the body was not read, so the connection must not be reused
    # The token still works, and a normal body is accepted.
    assert client.call("POST", "/v0/events", {"events": []})[0] == 200


def test_pairing_rejects_a_bad_length_without_spending_a_guess(server):
    _, info, client, _ = server
    huge = _post_headers("/v0/pair", f"Content-Length: {serve.MAX_PAIR_BODY + 1}")
    negative = _post_headers("/v0/pair", "Content-Length: -1")
    assert _status_line(_raw_exchange(info["port"], info["fingerprint"], huge)) == 413
    assert _status_line(_raw_exchange(info["port"], info["fingerprint"], negative)) == 400
    wrong = "000000" if info["code"] != "000000" else "111111"
    status, body = client.call("POST", "/v0/pair", {"code": wrong, "device_id": PHONE, "name": "Example iPhone"})
    assert status == 403 and body["details"]["tries_left"] == serve.PAIRING_TRIES - 1
    _pair(client, info)


def test_a_stalled_client_is_disconnected(server, monkeypatch):
    monkeypatch.setattr(serve, "SOCKET_TIMEOUT_S", 1)
    _, info, client, _ = server
    started = time.monotonic()
    data = _raw_exchange(info["port"], info["fingerprint"],
                         b"POST /v0/events HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 10\r\n", timeout=4)
    assert time.monotonic() - started < 5
    assert not data.endswith(b"<timeout>")
    assert b"HTTP/1.1 200" not in data
    assert client.call("GET", "/v0/hello")[0] == 200


def test_a_slow_body_is_stopped(server, monkeypatch):
    monkeypatch.setattr(serve, "REQUEST_DEADLINE_S", 1)
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    head = _post_headers("/v0/events", f"Authorization: Bearer {token}", "Content-Length: 100000")
    # Headers plus one body byte, then nothing. The declared length is in range, so only the deadline can stop it.
    data = _raw_exchange(info["port"], info["fingerprint"], head + b"{", timeout=5)
    assert _status_line(data) == 408
    assert _json_body(data)["error"] == "timeout"
    assert client.call("GET", "/v0/status")[0] == 200


def test_plan_and_status_give_up_on_a_long_query(server, monkeypatch):
    _, info, client, _ = server
    _pair(client, info)
    monkeypatch.setattr(serve, "QUERY_TIMEOUT_S", 0)
    monkeypatch.setattr(serve, "QUERY_PROGRESS_INTERVAL", 1)
    for path in ("/v0/plan", "/v0/status"):
        status, body = client.call("GET", path)
        assert status == 503 and body["error"] == "timeout", path
        assert contract.validate_sync("error", body) == []
    assert client.call("GET", "/v0/hello")[0] == 200


def test_pushed_events_cannot_name_another_actor(server, byobot_home):
    _, info, client, _ = server
    _pair(client, info)
    mine = _event("activity.defined", {"id": "plank", "name": "Plank"})
    agent = {**_event("activity.defined", {"id": "curl", "name": "Curl"}),
             "actor": {"kind": "agent", "id": "example-agent", "model": "example-model-1"}}
    other = {**_event("activity.defined", {"id": "run", "name": "Run"}),
             "actor": {"kind": "human", "id": "example-laptop"}}
    status, body = client.call("POST", "/v0/events", {"events": [mine, agent, other]})
    assert status == 200 and contract.validate_sync("events_response", body) == []
    assert [r["status"] for r in body["results"]] == ["accepted", "rejected", "rejected"]
    assert body["results"][1]["errors"][0]["path"] == "$.actor"
    assert body["results"][2]["errors"][0]["path"] == "$.actor"
    assert client.call("POST", "/v0/events", {"events": [mine]})[1]["results"][0]["status"] == "duplicate"
    assert client.call("POST", "/v0/events", {"events": [agent]})[1]["results"][0]["errors"][0]["path"] == "$.actor"
    with Store(byobot_home / "byobot.db") as s:
        assert s.rows("SELECT actor_kind, actor_id, actor_model FROM activities") == [
            {"actor_kind": "human", "actor_id": "me", "actor_model": None}]


def test_a_nested_body_is_rejected(server):
    _, info, client, _ = server
    token = _pair(client, info)["token"]
    nested = b"[" * 2000 + b"]" * 2000
    request = _post_headers("/v0/events", f"Authorization: Bearer {token}", f"Content-Length: {len(nested)}") + nested
    data = _raw_exchange(info["port"], info["fingerprint"], request)
    assert _status_line(data) == 400 and _json_body(data)["error"] == "invalid"
    assert client.call("GET", "/v0/status")[0] == 200


def test_plan_window_brings_the_last_two_times_at_each_location(store):
    def logged(i, day, location):
        store.accept({"id": f"0193a0b0-0000-7000-8000-0000000005{i:02d}", "schema_version": 0, "type": "session.performed",
                      "ts": f"2026-11-{day}T09:00:00Z", "device_id": "phone-1", "actor": {"kind": "human", "id": "me"},
                      "payload": {"id": f"s-{i}", "started_at": f"2026-11-{day}T08:00:00Z", "location_id": location,
                                  "efforts": [{"id": f"s-{i}-{n}", "activity_id": "barbell_back_squat", "round": n,
                                               "metrics": {"load": {"value": 90 + i, "unit": "kg"}}}
                                              for n in (1, 2)]}})

    for i, (day, location) in enumerate([("10", "home"), ("12", "home"), ("14", "home"), ("15", "hotel-gym")]):
        logged(i, day, location)
    store.accept(case("event/session-planned-at-location"))
    window = serve.plan_window(store, "2026-11-22", "2026-11-30")
    assert contract.validate_sync("plan_window", window) == []
    assert [(e["id"], e["location_id"]) for e in window["efforts"]] == [
        ("s-1-1", "home"), ("s-1-2", "home"), ("s-2-1", "home"), ("s-2-2", "home"),
        ("s-3-1", "hotel-gym"), ("s-3-2", "hotel-gym")]
