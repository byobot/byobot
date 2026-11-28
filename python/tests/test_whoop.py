from __future__ import annotations

import http.client
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from byobot import credentials, oauth, whoop
from byobot.commands import Context, run
from byobot.util import NotConnected, ValidationFailed


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
    assert query["redirect_uri"] == whoop.REDIRECT and query["scope"] == whoop.SCOPES and len(query["state"]) >= 43
    form = fake.requests[0][3]
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["example-code"]
    assert form["client_secret"] == ["example-secret"] and form["redirect_uri"] == [whoop.REDIRECT]
    assert query["code_challenge_method"] == "S256" and query["code_challenge"] == oauth.pkce_challenge(
        form["code_verifier"][0])
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


def test_a_refresh_another_process_already_made_is_used_not_repeated():
    connected(expires_at=time.time() - 1)
    import_ = whoop.Client(Fake({}))  # loaded before another process refreshed
    connected(access_token="access-2", refresh_token="refresh-2")
    fake = Fake({("GET", "/developer/v2/cycle"): [{"records": []}]})  # no token answer: a post would fail
    import_.opener = fake
    import_.request("GET", "/v2/cycle")
    assert fake.requests[0][4]["Authorization"] == "Bearer access-2"
    assert credentials.load(whoop.NAME)["refresh_token"] == "refresh-2"


def test_two_refreshing_at_once_spend_the_refresh_token_once():
    connected(expires_at=time.time() - 1)
    posts = []

    def opener(req):
        if req.get_method() == "POST":
            posts.append(urllib.parse.parse_qs(req.data.decode())["refresh_token"])
            time.sleep(0.2)  # the other one tries meanwhile
            return io.BytesIO(json.dumps(tokens(len(posts) + 1)).encode())
        return io.BytesIO(b'{"records": []}')
    clients = [whoop.Client(opener) for _ in range(2)]
    threads = [threading.Thread(target=c.request, args=("GET", "/v2/cycle")) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert posts == [["refresh-1"]] and credentials.load(whoop.NAME)["refresh_token"] == "refresh-2"


def test_a_refused_token_is_refreshed_once():
    connected()
    fake = Fake({("GET", "/developer/v2/cycle"): [401, {"records": []}], ("POST", "/oauth/oauth2/token"): [tokens(2)]})
    assert whoop.Client(fake).request("GET", "/v2/cycle") == {"records": []}


def test_a_request_has_a_timeout(monkeypatch):
    connected()
    seen = []

    def urlopen(req, timeout=None):
        seen.append(timeout)
        return io.BytesIO(b'{"records": []}')
    monkeypatch.setattr(whoop, "_urlopen", urlopen)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    whoop.Client().request("GET", "/v2/cycle")
    assert seen == [whoop.TIMEOUT]


def test_a_long_wait_or_a_refusal_is_not_tried_again(monkeypatch):
    connected()
    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("waited"))
    def busy(req):
        raise urllib.error.HTTPError(req.full_url, 429, "busy", {"Retry-After": "3600"}, None)
    with pytest.raises(whoop.UpstreamError, match="too many requests"):
        whoop.Client(busy).request("GET", "/v2/cycle")
    with pytest.raises(whoop.UpstreamError, match="answered 400"):
        whoop.Client(Fake({("GET", "/developer/v2/cycle"): [(400, "{}")]})).request("GET", "/v2/cycle")


def test_not_connected_says_how_to_connect():
    with pytest.raises(NotConnected, match="byobot connect whoop"):
        whoop.Client(Fake({}))


def test_disconnect_revokes_and_forgets():
    connected()
    fake = Fake({("DELETE", "/developer/v2/user/access"): [None]})
    assert whoop.disconnect(fake) == {"disconnected": True, "revoked_at_whoop": True}
    assert credentials.load(whoop.NAME) is None


def test_connect_needs_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(ValidationFailed, match="terminal"):
        run("connect", Context(actor={"kind": "human", "id": "cli"}), {"source": "whoop"})


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


def import_whoop(monkeypatch, fake, connect=True, **args):
    if connect:
        connected()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    ctx = Context(actor={"kind": "human", "id": "cli"})
    try:
        return run("import", ctx, {"source": "whoop", **args}), ctx
    finally:
        ctx.close()


def observations(store):
    return {r["code"]: r for r in store.rows("SELECT code, started_at, value FROM observations WHERE source = 'whoop'")}


def test_import_keeps_each_record_and_reads_it_toward_the_morning(monkeypatch, store):
    from byobot.review import review

    out, _ = import_whoop(monkeypatch, whoop_api([sleep()], [recovery()], [cycle()]))
    assert out["fetched"] == out["written"] == 3 and out["whoop_api"] == "2.0.0"
    raw = store.rows("SELECT source_type, source_id, raw FROM source_records ORDER BY source_type")
    assert [(r["source_type"], r["source_id"]) for r in raw] == [("cycle", "10"), ("recovery", "10"),
                                                                 ("sleep", SLEEP_ID)]
    assert json.loads(raw[1]["raw"]) == recovery()  # kept as WHOOP sent it
    found = observations(store)
    assert found["whoop_recovery_score"]["started_at"] == "2026-11-20T11:00:00.000Z"  # when you woke
    assert found["sleep_deep_duration"]["value"] == 5400 and found["oxygen_saturation"]["value"] == 96.5
    assert review(store, 1, "2026-11-22")["recovery"] == [
        {"date": "2026-11-20", "asleep": {"value": 7.5, "unit": "h"}, "hrv_rmssd": {"value": 61.2, "unit": "ms"},
         "oxygen_saturation": {"value": 96.5, "unit": "%"}, "respiratory_rate": {"value": 15.2, "unit": "/min"},
         "resting_heart_rate": {"value": 52, "unit": "/min"}, "skin_temperature": {"value": 33.4, "unit": "Cel"},
         "whoop_recovery_score": {"value": 68, "unit": "%"}, "whoop_sleep_performance": {"value": 91, "unit": "%"},
         "whoop_strain": {"value": 12.7}}]
    assert {r["actor_kind"] for r in store.rows("SELECT actor_kind FROM events")} == {"connector"}


def test_import_again_changes_nothing_and_starts_a_week_before_the_last(monkeypatch, store):
    import_whoop(monkeypatch, whoop_api([sleep()], [recovery()], [cycle()]))
    fake = whoop_api([sleep()], [recovery()], [cycle()])
    out, _ = import_whoop(monkeypatch, fake)
    assert out["fetched"] == 3 and out["written"] == 0
    assert out["since"] == "2026-11-13T03:00:00.000Z"
    assert all(r[2]["start"] == [out["since"]] for r in fake.requests)
    assert store.rows("SELECT count(*) AS n FROM events")[0]["n"] == 3


def test_a_rescore_is_a_new_version(monkeypatch, store):
    import_whoop(monkeypatch, whoop_api(recoveries=[recovery()]))
    rescored = recovery(updated_at="2026-11-20T15:00:00.000Z")
    rescored["score"] = {**rescored["score"], "recovery_score": 70.0}
    out, _ = import_whoop(monkeypatch, whoop_api(recoveries=[rescored]), days=30)
    assert out["written"] == 1 and observations(store)["whoop_recovery_score"]["value"] == 70
    rid = store.rows("SELECT id FROM source_records")[0]["id"]
    assert len(run("versions", Context(actor=YOU), {"kind": "source_record", "id": rid})["versions"]) == 2


def test_import_follows_next_token(monkeypatch, store):
    fake = whoop_api()
    fake.answers[("GET", "/developer/v2/activity/sleep")] = [
        {"records": [sleep()], "next_token": "page-2"},
        {"records": [sleep(id="00000000-0000-4000-8000-000000000002", nap=True, score_state="PENDING_SCORE",
                           score=None)]}]
    out, _ = import_whoop(monkeypatch, fake, days=30)
    sleep_requests = [r[2] for r in fake.requests if r[1].endswith("/sleep")]
    assert sleep_requests[0]["limit"] == ["25"] and sleep_requests[1]["nextToken"] == ["page-2"]
    assert out["written"] == 2


def test_an_unscored_record_is_kept_but_reads_nothing(monkeypatch, store):
    out, _ = import_whoop(monkeypatch, whoop_api(recoveries=[recovery(score_state="UNSCORABLE", score=None)]))
    assert out["written"] == 1 and observations(store) == {}


def test_a_page_whoop_changed_is_refused_before_anything_is_written(monkeypatch, store):
    renamed = sleep()
    renamed["score"]["stage_summary"]["total_slow_wave_sleep_time_milli"] = "5400000"
    with pytest.raises(whoop.UpstreamError, match="stage_summary/total_slow_wave_sleep_time_milli"):
        import_whoop(monkeypatch, whoop_api([renamed]))
    assert store.rows("SELECT count(*) AS n FROM source_records")[0]["n"] == 0


def workouts(store):
    return run("list", Context(actor=YOU), {"kind": "workouts"})["items"]


def test_workouts_are_listed_once_with_their_metrics(monkeypatch, store):
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    [found] = workouts(store)
    assert found["name"] == "running" and [i["source"] for i in found["items"]] == ["whoop"]
    assert found["metrics"]["duration"]["value"] == 2700 and found["metrics"]["heart_rate"]["value"] == 148
    assert found["metrics"]["active_energy"]["value"] == 2092


@pytest.mark.parametrize("code, name", [("weightlifting_msk", "strength training"),
                                        ("mountain-biking", "mountain biking"), ("some_new_sport", "some new sport")])
def test_sports_get_readable_names(monkeypatch, store, code, name):
    import_whoop(monkeypatch, whoop_api(workouts=[workout(sport_name=code)]))
    assert workouts(store)[0]["name"] == name


YOU = {"kind": "human", "id": "cli"}


def log_session(**overrides):
    """A session logged in byobot that overlaps workout() by 40 minutes, with one set."""
    sid = "01a2147e-0000-7000-8000-0000000000aa"
    session = {"kind": "session", "id": sid, "payload": {
        "name": "Pull", "time": {"start": "2026-11-20T17:05:00Z", "end": "2026-11-20T17:50:00Z"}, **overrides}}
    effort = {"kind": "effort", "payload": {"activity_id": "barbell_row", "time": {"start": "2026-11-20T17:10:00Z"},
                                            "links": {"part_of": {"kind": "session", "id": sid}},
                                            "metrics": {"reps": {"value": 8}}}}
    ctx = Context(actor=YOU)
    run("log", ctx, {"kind": "batch", "payload": [session, effort]})
    ctx.close()
    return sid


def test_a_workout_over_a_session_you_logged_is_one_workout(monkeypatch, store):
    from byobot.review import review

    sid = log_session(metrics={"duration": {"value": 3000, "unit": "s"}})
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    [found] = workouts(store)
    assert found["name"] == "Pull" and {i["kind"] for i in found["items"]} == {"session", "source_record"}
    assert found["metrics"]["duration"]["value"] == 3000 and found["metrics"]["duration"]["from"]["id"] == sid
    assert found["metrics"]["heart_rate"]["value"] == 148  # what you didn't record comes from WHOOP
    reviewed = review(store, 1, "2026-11-22")["sessions"]
    assert len(reviewed) == 1 and reviewed[0]["sets"] == 1 and reviewed[0]["sources"] == ["whoop"]

    changed = workout(updated_at="2026-11-21T00:00:00.000Z")
    changed["score"] = {**changed["score"], "average_heart_rate": 150}
    import_whoop(monkeypatch, whoop_api(workouts=[changed]), days=30)
    [found] = workouts(store)
    assert found["metrics"]["heart_rate"]["value"] == 150 and found["metrics"]["duration"]["value"] == 3000


def test_a_workout_imported_before_you_logged_merges_too(monkeypatch, store):
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    log_session()
    assert len(workouts(store)) == 1


def test_a_workout_that_doesnt_overlap_stays_its_own(monkeypatch, store):
    log_session(time={"start": "2026-11-20T18:00:00Z", "end": "2026-11-20T19:00:00Z"})
    import_whoop(monkeypatch, whoop_api(workouts=[workout()]))
    assert sorted(w["name"] for w in workouts(store)) == ["Pull", "running"]


def test_the_whoop_sign_in_state_has_at_least_128_bits():
    seen = []

    def wait(state):
        seen.append(state)
        raise RuntimeError("stop before the token request")

    with pytest.raises(RuntimeError):
        whoop.connect("example-client", "example-secret", open_browser=lambda url: None, wait=wait)
    assert len(seen[0]) >= 22


UTC = timezone.utc


def stamp(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sleep_record(n: int, night: datetime) -> dict:
    start, end = night, night + timedelta(hours=8)
    return {"user_id": 1, "created_at": stamp(end), "updated_at": stamp(end), "timezone_offset": "-05:00",
            "id": f"00000000-0000-4000-8000-{n:012d}", "cycle_id": n, "start": stamp(start), "end": stamp(end),
            "nap": False, "score_state": "SCORED", "score": {
                "stage_summary": {"total_in_bed_time_milli": 28800000, "total_awake_time_milli": 1800000,
                                  "total_no_data_time_milli": 0, "total_light_sleep_time_milli": 12600000,
                                  "total_slow_wave_sleep_time_milli": 5400000, "total_rem_sleep_time_milli": 9000000,
                                  "sleep_cycle_count": 4, "disturbance_count": 8},
                "sleep_needed": {"baseline_milli": 27000000, "need_from_sleep_debt_milli": 0,
                                 "need_from_recent_strain_milli": 0, "need_from_recent_nap_milli": 0},
                "respiratory_rate": 15.2, "sleep_performance_percentage": 91.0}}


def recovery_record(n: int, night: datetime) -> dict:
    end = night + timedelta(hours=8)
    return {"user_id": 1, "created_at": stamp(end), "updated_at": stamp(end), "cycle_id": n,
            "sleep_id": f"00000000-0000-4000-8000-{n:012d}", "score_state": "SCORED", "score": {
                "user_calibrating": False, "recovery_score": 60.0 + n % 30, "resting_heart_rate": 52.0,
                "hrv_rmssd_milli": 61.24, "spo2_percentage": 96.5, "skin_temp_celsius": 33.4}}


def cycle_record(n: int, night: datetime) -> dict:
    return {"user_id": 1, "created_at": stamp(night), "updated_at": stamp(night), "timezone_offset": "-05:00",
            "id": n, "start": stamp(night), "end": stamp(night + timedelta(days=1)), "score_state": "SCORED",
            "score": {"strain": 10.5, "kilojoule": 9000.0, "average_heart_rate": 70, "max_heart_rate": 160}}


def workout_record(n: int, at: datetime) -> dict:
    return {"user_id": 1, "created_at": stamp(at), "updated_at": stamp(at), "timezone_offset": "-05:00",
            "id": f"00000000-0000-4000-9000-{n:012d}", "start": stamp(at), "end": stamp(at + timedelta(minutes=45)),
            "sport_name": "running", "sport_id": 0, "score_state": "SCORED",
            "score": {"strain": 9.1, "average_heart_rate": 148, "max_heart_rate": 171, "kilojoule": 2092.0,
                      "percent_recorded": 100.0, "distance_meter": 7000.0, "altitude_gain_meter": None,
                      "altitude_change_meter": None, "zone_durations": {f"zone_{z}_milli": 0 for z in (
                          "zero", "one", "two", "three", "four", "five")}}}


def history(days: int, until: datetime, workouts_every: int = 3) -> dict[str, list[dict]]:
    """`days` nights of sleep, recovery and cycles before `until`, and a run every few days."""
    out: dict[str, list[dict]] = {"sleep": [], "recovery": [], "cycle": [], "workout": []}
    for n in range(1, days + 1):
        night = until - timedelta(days=days - n + 1, hours=-3)  # 03:00 UTC, 22:00 at -05:00
        out["sleep"].append(sleep_record(n, night))
        out["recovery"].append(recovery_record(n, night))
        out["cycle"].append(cycle_record(n, night))
        if n % workouts_every == 0:
            out["workout"].append(workout_record(n, night + timedelta(hours=14)))
    return out


PATHS = {"/developer/v2/activity/sleep": "sleep", "/developer/v2/recovery": "recovery",
         "/developer/v2/cycle": "cycle", "/developer/v2/activity/workout": "workout"}


class FakeWhoop:
    """Answers GETs like WHOOP: newest first, `limit` per page, `nextToken` to go on. `per_minute` requests are
    allowed per minute of the fake clock (WHOOP's documented limit is 100); over it a 429 says to retry after the
    rest of the minute. `fail` maps a request number (1-based) to an HTTP status to answer once instead."""

    def __init__(self, records: dict[str, list[dict]], per_minute: int | None = None, fail: dict | None = None):
        self.records = records
        self.per_minute = per_minute
        self.fail = dict(fail or {})
        self.clock = time.time()
        self.window: list[float] = []
        self.requests = 0
        self.answered = 0

    def sleep(self, seconds: float) -> None:
        self.clock += max(0.0, float(seconds))

    def time(self) -> float:
        return self.clock

    def __call__(self, req):
        self.requests += 1
        url = urllib.parse.urlsplit(req.full_url)
        if req.get_method() == "POST":
            raise AssertionError("no sign-in expected")
        if self.requests in self.fail:
            code = self.fail.pop(self.requests)
            raise urllib.error.HTTPError(req.full_url, code, "error", {"Retry-After": "1"},
                                         io.BytesIO(b'{"message": "try again"}'))
        if self.per_minute is not None:
            self.window = [t for t in self.window if t > self.clock - 60]
            if len(self.window) >= self.per_minute:
                wait = int(self.window[0] + 60 - self.clock) + 1
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": str(wait)},
                                             io.BytesIO(b'{"message": "Too Many Requests"}'))
            self.window.append(self.clock)
        query = dict(urllib.parse.parse_qsl(url.query))
        kind = PATHS[url.path]
        start = query.get("start")
        found = [r for r in self.records[kind] if start is None or (r.get("start") or r["created_at"]) >= start]
        found.sort(key=lambda r: r.get("start") or r["created_at"], reverse=True)
        offset = int(query.get("nextToken", "0"))
        limit = int(query.get("limit", "25"))
        page = found[offset:offset + limit]
        body = {"records": page}
        if offset + limit < len(found):
            body["next_token"] = str(offset + limit)
        self.answered += 1
        return io.BytesIO(json.dumps(body).encode())


UNTIL = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _fake_clock(monkeypatch, fake: FakeWhoop) -> None:
    """Waiting moves the fake clock on instead of the test's."""
    monkeypatch.setattr(time, "sleep", fake.sleep)
    monkeypatch.setattr(time, "time", fake.time)


def _held(store) -> int:
    return store.rows("SELECT count(*) AS n FROM source_records WHERE source = 'whoop'")[0]["n"]


def test_conn_a_long_history_import_finishes_under_whoops_rate_limit(monkeypatch, store):
    records = history(900, UNTIL)  # about 120 requests: more than WHOOP answers in one minute
    fake = FakeWhoop(records, per_minute=100)
    _fake_clock(monkeypatch, fake)
    out, _ = import_whoop(monkeypatch, fake, days=1000)
    total = sum(len(v) for v in records.values())
    assert out["fetched"] == total
    assert _held(store) == total


@pytest.mark.parametrize("status", [429, 502, 503])
def test_conn_one_busy_answer_mid_import_is_tried_again(monkeypatch, store, status):
    records = history(60, UNTIL)
    fake = FakeWhoop(records, fail={3: status})  # the third page answers busy once
    _fake_clock(monkeypatch, fake)
    out, _ = import_whoop(monkeypatch, fake, days=90)
    total = sum(len(v) for v in records.values())
    assert out["fetched"] == total and _held(store) == total


class _NoRotation(FakeWhoop):
    """A token endpoint that renews the access token without sending a new refresh token, as RFC 6749 section
    6 allows; the refresh token sent stays good."""

    def __call__(self, req):
        if req.get_method() == "POST":
            self.refreshed = True
            return io.BytesIO(json.dumps({"access_token": "access-2", "expires_in": 3600,
                                          "token_type": "bearer"}).encode())
        return super().__call__(req)


def test_conn_a_refresh_without_a_new_refresh_token_keeps_the_old_one(monkeypatch, store):
    connected(expires_at=time.time() - 10)  # the access token has run out
    records = history(3, UNTIL)
    fake = _NoRotation(records)
    try:
        out, _ = import_whoop(monkeypatch, fake, connect=False, days=30)
    except KeyError as exc:  # on main: a bare KeyError from whoop._tokens, not an answer
        pytest.fail(f"import crashed with KeyError {exc}")
    assert out["fetched"] == sum(len(v) for v in records.values())
    kept = credentials.load(whoop.NAME)
    assert kept["access_token"] == "access-2" and kept["refresh_token"] == "refresh-1"


SIGNED_IN = {"access_token": "t", "refresh_token": "r", "expires_at": 1e12, "client_id": "c", "client_secret": "s"}


def signed_in(monkeypatch, opener):
    monkeypatch.setattr(whoop.credentials, "load", lambda name: dict(SIGNED_IN))
    return whoop.Client(opener)


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_page_cut_short_is_tried_again_then_an_upstream_error(monkeypatch):
    monkeypatch.setattr(whoop.time, "sleep", lambda s: None)

    class Cut(Answer):
        def read(self, *a):
            raise http.client.IncompleteRead(b"{")

    client = signed_in(monkeypatch, lambda req: Cut(b""))
    with pytest.raises(whoop.UpstreamError):
        client.request("GET", "/v2/cycle")


def test_paging_stops_when_the_same_next_token_comes_back(monkeypatch):
    calls = []

    def opener(req):
        calls.append(req.full_url)
        if len(calls) > 20:
            raise AssertionError("still paging after 20 requests")
        return Answer(json.dumps({"records": [], "next_token": "same"}).encode())

    client = signed_in(monkeypatch, opener)
    with pytest.raises(whoop.UpstreamError):
        list(client.pages("/v2/cycle", "PaginatedCycleResponse", "2026-03-01T00:00:00.000Z"))
    assert len(calls) == 2
