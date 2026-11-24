import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from byobot import credentials, whoop
from byobot.commands import Context, run
from byobot.util import ValidationFailed


@pytest.fixture(autouse=True)
def file_credentials(monkeypatch):
    monkeypatch.setenv("BYOBOT_CREDENTIALS", "file")


class Fake:
    """Answers like WHOOP, from canned answers per (method, path)."""

    def __init__(self, answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.requests = []

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        body = urllib.parse.parse_qs(req.data.decode()) if req.data else None
        self.requests.append((req.get_method(), url.path, urllib.parse.parse_qs(url.query), body,
                              dict(req.header_items())))
        answer = self.answers[(req.get_method(), url.path)].pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "error", {}, None)
        if isinstance(answer, tuple):
            raise urllib.error.HTTPError(req.full_url, answer[0], "error", {}, io.BytesIO(answer[1].encode()))
        return io.BytesIO(json.dumps(answer).encode() if answer is not None else b"")


def tokens(n):
    return {"access_token": f"access-{n}", "refresh_token": f"refresh-{n}", "expires_in": 3600,
            "scope": whoop.SCOPES, "token_type": "bearer"}


def connected(**overrides):
    stored = {"client_id": "example-client", "client_secret": "example-secret", "access_token": "access-1",
              "refresh_token": "refresh-1", "expires_at": time.time() + 3600, **overrides}
    credentials.save(whoop.NAME, stored)
    return stored


def test_connect_signs_in_and_keeps_the_tokens():
    fake = Fake({("POST", "/oauth/oauth2/token"): [tokens(1)]})
    opened = []
    out = whoop.connect("example-client", "example-secret", opener=fake, open_browser=opened.append,
                        wait=lambda state: "example-code")
    assert out == {"connected": True, "scopes": whoop.SCOPES.split()}
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
    assert query["redirect_uri"] == whoop.REDIRECT and query["scope"] == whoop.SCOPES and len(query["state"]) == 8
    form = fake.requests[0][3]
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["example-code"]
    assert form["client_secret"] == ["example-secret"] and form["redirect_uri"] == [whoop.REDIRECT]
    assert credentials.load(whoop.NAME)["refresh_token"] == "refresh-1"
    assert fake.requests[0][4]["User-agent"].startswith("byobot/")


@pytest.mark.parametrize("body, why", [
    ('{"error": "invalid_client", "error_description": "Client authentication failed"}',
     "invalid_client Client authentication failed"),
    ("<html><title>Attention Required! | Cloudflare</title>error code: 1010</html>",
     "turned away by WHOOP's firewall (Cloudflare 1010)"),
])
def test_a_refused_sign_in_says_why(body, why):
    fake = Fake({("POST", "/oauth/oauth2/token"): [(403, body)]})
    with pytest.raises(whoop.UpstreamError, match=re.escape(f"(403: {why})")):
        whoop.connect("example-client", "example-secret", opener=fake, open_browser=lambda url: None,
                      wait=lambda state: "example-code")


def test_the_redirect_is_caught_on_this_machine(monkeypatch):
    monkeypatch.setattr(whoop, "PORT", 0)
    port = {}
    real = whoop.http.server.HTTPServer

    class Server(real):
        def server_activate(self):
            super().server_activate()
            port["n"] = self.server_address[1]

    monkeypatch.setattr(whoop.http.server, "HTTPServer", Server)
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=whoop.wait_for_code("abcd1234", timeout=10)))
    thread.start()
    while "n" not in port:
        time.sleep(0.01)
    url = f"http://127.0.0.1:{port['n']}/whoop/callback?code=example-code&state=abcd1234"
    with urllib.request.urlopen(url) as resp:
        assert b"connected" in resp.read()
    thread.join()
    assert result["code"] == "example-code"


def test_an_expired_token_is_refreshed_and_the_new_one_kept():
    connected(expires_at=time.time() - 1)
    fake = Fake({("POST", "/oauth/oauth2/token"): [tokens(2)], ("GET", "/developer/v2/cycle"): [{"records": []}]})
    whoop.Client(fake).request("GET", "/v2/cycle", {"limit": 1})
    form = fake.requests[0][3]
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["refresh-1"]
    assert fake.requests[1][4]["Authorization"] == "Bearer access-2"
    assert fake.requests[1][4]["User-agent"].startswith("byobot/")
    assert credentials.load(whoop.NAME)["refresh_token"] == "refresh-2"


def test_a_refused_token_is_refreshed_once():
    connected()
    fake = Fake({("GET", "/developer/v2/cycle"): [401, {"records": []}], ("POST", "/oauth/oauth2/token"): [tokens(2)]})
    assert whoop.Client(fake).request("GET", "/v2/cycle") == {"records": []}


def test_not_connected_says_how_to_connect():
    with pytest.raises(whoop.NotConnected, match="byobot connect whoop"):
        whoop.Client(Fake({}))


def test_disconnect_revokes_and_forgets():
    connected()
    fake = Fake({("DELETE", "/developer/v2/user/access"): [None]})
    assert whoop.disconnect(fake) == {"disconnected": True, "revoked_at_whoop": True}
    assert credentials.load(whoop.NAME) is None


def test_connect_needs_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(ValidationFailed, match="terminal"):
        run("connect whoop", Context(actor={"kind": "human", "id": "cli"}), {})


SLEEP_ID = "00000000-0000-4000-8000-000000000001"
STAMP = {"user_id": 1, "created_at": "2026-11-20T11:30:00.000Z", "updated_at": "2026-11-20T11:30:00.000Z",
         "timezone_offset": "+00:00"}


def sleep(**overrides):
    return {**STAMP, "id": SLEEP_ID, "cycle_id": 10, "start": "2026-11-20T03:00:00.000Z",
            "end": "2026-11-20T11:00:00.000Z", "nap": False, "score_state": "SCORED", "score": {
                "stage_summary": {"total_in_bed_time_milli": 28800000, "total_awake_time_milli": 1800000,
                                  "total_no_data_time_milli": 0, "total_light_sleep_time_milli": 12600000,
                                  "total_slow_wave_sleep_time_milli": 5400000, "total_rem_sleep_time_milli": 9000000,
                                  "sleep_cycle_count": 4, "disturbance_count": 8},
                "sleep_needed": {"baseline_milli": 27000000, "need_from_sleep_debt_milli": 0,
                                 "need_from_recent_strain_milli": 0, "need_from_recent_nap_milli": 0},
                "respiratory_rate": 15.2, "sleep_performance_percentage": 91.0}, **overrides}


def recovery(**overrides):
    return {**STAMP, "cycle_id": 10, "sleep_id": SLEEP_ID, "score_state": "SCORED", "score": {
        "user_calibrating": False, "recovery_score": 68.0, "resting_heart_rate": 52.0, "hrv_rmssd_milli": 61.24,
        "spo2_percentage": 96.5, "skin_temp_celsius": 33.4}, **overrides}


def cycle(**overrides):
    return {**STAMP, "id": 10, "start": "2026-11-20T03:00:00.000Z", "end": None, "score_state": "SCORED",
            "score": {"strain": 12.7, "kilojoule": 9000.0, "average_heart_rate": 70, "max_heart_rate": 160},
            **overrides}


def workout(**overrides):
    return {**STAMP, "id": "00000000-0000-4000-8000-000000000003", "start": "2026-11-20T17:00:00.000Z",
            "end": "2026-11-20T17:45:00.000Z", "sport_name": "running", "sport_id": 0, "score_state": "SCORED",
            "score": {"strain": 9.1, "average_heart_rate": 148, "max_heart_rate": 171, "kilojoule": 2092.0,
                      "percent_recorded": 100.0, "distance_meter": 7000.0, "altitude_gain_meter": None,
                      "altitude_change_meter": None, "zone_durations": {f"zone_{z}_milli": 0 for z in (
                          "zero", "one", "two", "three", "four", "five")}}, **overrides}


def whoop_api(sleeps=(), recoveries=(), cycles=(), workouts=()):
    page = lambda records: [{"records": list(records)}]
    return Fake({("GET", "/developer/v2/activity/sleep"): page(sleeps),
                 ("GET", "/developer/v2/recovery"): page(recoveries),
                 ("GET", "/developer/v2/cycle"): page(cycles),
                 ("GET", "/developer/v2/activity/workout"): page(workouts)})


def import_whoop(monkeypatch, fake, **args):
    connected()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    ctx = Context(actor={"kind": "human", "id": "cli"})
    try:
        return run("import whoop", ctx, args), ctx
    finally:
        ctx.close()


def test_import_maps_recovery_sleep_and_strain_to_the_morning(monkeypatch, store):
    from byobot.review import review

    out, _ = import_whoop(monkeypatch, whoop_api([sleep()], [recovery()], [cycle()]))
    assert out["telemetry"] == {"inserted": 13, "updated": 0, "unchanged": 0} and out["skipped"] == []
    assert out["whoop_api"] == "2.0.0"
    rows = {r["type"]: r for r in store.rows("SELECT type, start_at, end_at, value FROM telemetry_samples")}
    assert rows["byobot.RecoveryScore"]["start_at"] == rows["byobot.Strain"]["start_at"] == "2026-11-20T11:00:00.000Z"
    assert rows["byobot.SleepDeepDuration"]["value"] == 5400
    assert rows["HKQuantityTypeIdentifierOxygenSaturation"]["value"] == 96.5
    assert review(store, 1, "2026-11-22")["recovery"] == [
        {"date": "2026-11-20", "asleep_h": 7.5, "hrv_rmssd_ms": 61.2, "recovery_pct": 68, "resting_hr_bpm": 52,
         "sleep_pct": 91, "strain": 12.7}]


def test_import_again_changes_nothing_and_starts_a_week_before_the_last(monkeypatch):
    import_whoop(monkeypatch, whoop_api([sleep()], [recovery()], [cycle()]))
    fake = whoop_api([sleep()], [recovery()], [cycle()])
    out, _ = import_whoop(monkeypatch, fake)
    assert out["telemetry"] == {"inserted": 0, "updated": 0, "unchanged": 13}
    assert out["since"] == "2026-11-13T11:00:00.000Z"
    assert all(r[2]["start"] == [out["since"]] for r in fake.requests)


def test_import_follows_next_token(monkeypatch):
    fake = whoop_api()
    fake.answers[("GET", "/developer/v2/activity/sleep")] = [
        {"records": [sleep()], "next_token": "page-2"},
        {"records": [sleep(id="00000000-0000-4000-8000-000000000002", nap=True, score_state="PENDING_SCORE",
                           score=None)]}]
    out, _ = import_whoop(monkeypatch, fake, days=30)
    sleep_requests = [r[2] for r in fake.requests if r[1].endswith("/sleep")]
    assert sleep_requests[0]["limit"] == ["25"] and sleep_requests[1]["nextToken"] == ["page-2"]
    assert out["telemetry"]["inserted"] == 8


def test_unscored_and_out_of_range_records_are_skipped(monkeypatch):
    bad = cycle(id=11, score={"strain": 30.0, "kilojoule": 1.0, "average_heart_rate": 1, "max_heart_rate": 1})
    out, _ = import_whoop(monkeypatch, whoop_api(recoveries=[recovery(score_state="UNSCORABLE", score=None)],
                                                 cycles=[bad]))
    assert out["telemetry"]["inserted"] == 0
    assert out["skipped"] == ["byobot.Strain at 2026-11-20T03:00:00.000Z: 30.0 is greater than the maximum of 21"]


def test_a_page_whoop_changed_is_refused_before_anything_is_written(monkeypatch, store):
    renamed = sleep()
    renamed["score"]["stage_summary"]["total_slow_wave_sleep_time_milli"] = "5400000"
    with pytest.raises(whoop.UpstreamError, match="stage_summary/total_slow_wave_sleep_time_milli"):
        import_whoop(monkeypatch, whoop_api([renamed]))
    assert store.rows("SELECT count(*) AS n FROM telemetry_samples")[0]["n"] == 0


def test_workouts_become_sessions(monkeypatch, store):
    out, _ = import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    assert out["sessions"] == {"added": 1, "updated": 0, "unchanged": 0}
    session = store.rows("SELECT * FROM performed_sessions")[0]
    assert session["id"] == "whoop:workout:00000000-0000-4000-8000-000000000003" and session["name"] == "running"
    metrics = store.metrics_of("performed_session", session["id"])
    assert metrics["duration"]["value"] == 2700 and metrics["heart_rate"]["value"] == 148
    assert metrics["x.calories"] == {"value": 500.0, "unit": "kcal"} and metrics["x.strain"]["value"] == 9.1
    assert metrics["x.whoop_sport"] == {"value": "running"}
    event = store.rows("SELECT actor_kind, actor_id FROM events")[0]
    assert (event["actor_kind"], event["actor_id"]) == ("connector", "whoop")
    again, _ = import_whoop(monkeypatch, whoop_api(workouts=[workout()]), days=30)
    assert again["sessions"] == {"added": 0, "updated": 0, "unchanged": 1}


@pytest.mark.parametrize("code, name", [("weightlifting_msk", "strength training"),
                                        ("mountain-biking", "mountain biking"), ("some_new_sport", "some new sport")])
def test_sports_get_readable_names(code, name):
    assert whoop.sport(code) == name


def test_your_undo_stands_until_whoop_changes_the_workout(monkeypatch, store):
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    event = store.rows("SELECT id FROM events")[0]["id"]
    store.append("event.undone", {"event_id": event}, {"kind": "human", "id": "cli"})
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]), days=30)
    assert store.rows("SELECT count(*) AS n FROM performed_sessions")[0]["n"] == 0
    out, _ = import_whoop(monkeypatch, whoop_api(workouts=[workout(sport_name="cycling")]), days=30)
    assert out["sessions"] == {"added": 0, "updated": 1, "unchanged": 0}
    assert store.rows("SELECT name FROM performed_sessions")[0]["name"] == "cycling"
