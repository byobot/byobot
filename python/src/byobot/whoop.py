"""Talk to WHOOP's API directly, with your own WHOOP developer app. No server sits in between.

`byobot connect whoop` signs you in once. The client secret and the tokens stay in
your Keychain (see credentials.py), and the import refreshes them itself. Every page
WHOOP sends is checked against the pinned API (whoop.schema.json) before anything is
written.
"""

from __future__ import annotations

import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any, Callable, Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from byobot import __version__, contract, credentials
from byobot.util import ByobotError

API = "https://api.prod.whoop.com/developer"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
PORT = 8766
REDIRECT = f"http://localhost:{PORT}/whoop/callback"
SCOPES = "offline read:recovery read:cycles read:sleep read:workout"
NAME = "whoop"
# Firewalls in front of APIs often turn away urllib's default User-Agent.
HEADERS = {"User-Agent": f"byobot/{__version__} (+https://github.com/byobot/byobot)", "Accept": "application/json"}
SNAPSHOT = Path(__file__).with_name("whoop.schema.json")
PAGE = 25


class UpstreamError(ByobotError):
    code = "upstream"


class NotConnected(ByobotError):
    code = "not_connected"


def _post(url: str, form: dict, opener: Callable) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise UpstreamError(f"WHOOP refused the sign-in ({e.code}). Run `byobot connect whoop` again.") from None
    except (urllib.error.URLError, OSError) as e:
        raise UpstreamError(f"can't reach WHOOP: {getattr(e, 'reason', e)}") from None


def _tokens(client_id: str, client_secret: str, answer: dict) -> dict:
    return {"client_id": client_id, "client_secret": client_secret, "access_token": answer["access_token"],
            "refresh_token": answer["refresh_token"], "expires_at": time.time() + answer.get("expires_in", 3600)}


def authorize_url(client_id: str, state: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({"response_type": "code", "client_id": client_id,
                                                    "redirect_uri": REDIRECT, "scope": SCOPES, "state": state})


def wait_for_code(state: str, timeout: float = 300) -> str:
    """Catch WHOOP's redirect on this machine and return the authorization code."""
    found: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            if url.path != "/whoop/callback":
                self.send_response(404)
                self.end_headers()
                return
            found.update(query)
            ok = query.get("state") == state and "code" in query
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"byobot is connected to WHOOP. You can close this tab." if ok
                             else b"byobot couldn't connect to WHOOP. Check the terminal.")

        def log_message(self, *args: Any) -> None:
            pass

    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as server:
        server.timeout = 1
        deadline = time.monotonic() + timeout
        while not found and time.monotonic() < deadline:
            server.handle_request()
    if not found:
        raise UpstreamError("no answer from WHOOP's sign-in; run `byobot connect whoop` again")
    if found.get("state") != state:
        raise UpstreamError("the sign-in answer didn't match this request; run `byobot connect whoop` again")
    if "code" not in found:
        raise UpstreamError(f"WHOOP didn't sign you in: {found.get('error_description') or found.get('error')}")
    return found["code"]


def connect(client_id: str, client_secret: str, opener: Callable = urllib.request.urlopen,
            open_browser: Callable[[str], Any] = webbrowser.open, wait: Callable[[str], str] = wait_for_code) -> dict:
    state = secrets.token_hex(4)
    url = authorize_url(client_id, state)
    open_browser(url)
    code = wait(state)
    answer = _post(TOKEN_URL, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                               "client_id": client_id, "client_secret": client_secret}, opener)
    if "refresh_token" not in answer:
        raise UpstreamError("WHOOP gave no refresh token; check that your app has the offline scope")
    credentials.save(NAME, _tokens(client_id, client_secret, answer))
    return {"connected": True, "scopes": answer.get("scope", SCOPES).split()}


def disconnect(opener: Callable = urllib.request.urlopen) -> dict:
    stored = credentials.load(NAME)
    if stored is None:
        return {"disconnected": False}
    revoked = True
    try:
        Client(opener).request("DELETE", "/v2/user/access")
    except ByobotError:
        revoked = False
    credentials.delete(NAME)
    return {"disconnected": True, "revoked_at_whoop": revoked}


def version() -> str:
    return _snapshot()["x-whoop-api-version"]


@cache
def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text())


def check(body: Any, name: str) -> None:
    """Refuse a page that doesn't match the pinned WHOOP API."""
    schema = {**_snapshot(), "$ref": f"#/components/schemas/{name}"}
    error = best_match(Draft202012Validator(schema).iter_errors(body))
    if error:
        while error.context:
            error = best_match(error.context)
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise UpstreamError(f"WHOOP sent a {name} byobot doesn't recognize (at {where}: {error.message}). "
                            f"byobot reads WHOOP API {version()}.")


class Client:
    def __init__(self, opener: Callable | None = None):
        self.opener = opener or urllib.request.urlopen
        self.stored = credentials.load(NAME)
        if self.stored is None:
            raise NotConnected("byobot isn't connected to WHOOP yet; run `byobot connect whoop` in a terminal")

    def _refresh(self) -> None:
        s = self.stored
        answer = _post(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": s["refresh_token"],
                                   "client_id": s["client_id"], "client_secret": s["client_secret"],
                                   "scope": "offline"}, self.opener)
        # WHOOP rotates refresh tokens, so the new one is saved before anything else.
        self.stored = _tokens(s["client_id"], s["client_secret"], answer)
        credentials.save(NAME, self.stored)

    def request(self, method: str, path: str, params: dict | None = None) -> Any:
        if self.stored["expires_at"] - 60 < time.time():
            self._refresh()
        for attempt in (1, 2):
            query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
            req = urllib.request.Request(f"{API}{path}" + (f"?{query}" if query else ""), method=method,
                                         headers={**HEADERS, "Authorization": f"Bearer {self.stored['access_token']}"})
            try:
                with self.opener(req) as resp:
                    body = resp.read()
                    return json.loads(body.decode("utf-8")) if body else None
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self._refresh()
                    continue
                if e.code == 429:
                    raise UpstreamError("WHOOP says too many requests; try again in a minute") from None
                raise UpstreamError(f"WHOOP answered {e.code} for {path}") from None
            except (urllib.error.URLError, OSError) as e:
                raise UpstreamError(f"can't reach WHOOP: {getattr(e, 'reason', e)}") from None
            except json.JSONDecodeError:
                raise UpstreamError(f"WHOOP sent something other than JSON for {path}") from None

    def pages(self, path: str, name: str, start: str) -> Iterator[dict]:
        """Every record since `start`, page by page, each page checked before any record is used."""
        params = {"limit": PAGE, "start": start}
        while True:
            body = self.request("GET", path, params)
            check(body, name)
            yield from body.get("records") or []
            if not body.get("next_token"):
                return
            params["nextToken"] = body["next_token"]


def utc(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sample(type_: str, key: Any, start: str, end: str, value: Any, unit: str | None, raw: dict) -> dict:
    sample = {"source": NAME, "source_id": f"{type_}:{key}", "type": type_, "start": start, "end": end,
              "value": value, "raw": raw}
    return {**sample, "unit": unit} if unit else sample


STAGES = {
    "total_light_sleep_time_milli": "byobot.SleepLightDuration",
    "total_slow_wave_sleep_time_milli": "byobot.SleepDeepDuration",
    "total_rem_sleep_time_milli": "byobot.SleepREMDuration",
    "total_awake_time_milli": "byobot.SleepAwakeDuration",
}
RECOVERY = (
    ("recovery_score", "byobot.RecoveryScore", "%"),
    ("resting_heart_rate", "HKQuantityTypeIdentifierRestingHeartRate", "count/min"),
    ("hrv_rmssd_milli", "byobot.HeartRateVariabilityRMSSD", "ms"),
    ("spo2_percentage", "HKQuantityTypeIdentifierOxygenSaturation", "%"),
    ("skin_temp_celsius", "byobot.SkinTemperature", "degC"),
)


def from_sleep(record: dict) -> list[dict]:
    start, end = utc(record["start"]), utc(record["end"])
    samples = [_sample("HKCategoryTypeIdentifierSleepAnalysis", record["id"], start, end, "inBed", None, record)]
    score = record.get("score") if record["score_state"] == "SCORED" else None
    if not score:
        return samples
    for key, type_ in STAGES.items():
        if score["stage_summary"].get(key) is not None:
            samples.append(_sample(type_, record["id"], start, end, score["stage_summary"][key] / 1000, "s", record))
    for key, type_, unit in (("sleep_performance_percentage", "byobot.SleepPerformance", "%"),
                             ("respiratory_rate", "HKQuantityTypeIdentifierRespiratoryRate", "count/min")):
        if score.get(key) is not None:
            samples.append(_sample(type_, record["id"], end, end, score[key], unit, record))
    return samples


def from_recovery(record: dict, woke: str | None) -> list[dict]:
    score = record.get("score") if record["score_state"] == "SCORED" else None
    if not score:
        return []
    at = woke or utc(record["created_at"])
    return [_sample(type_, record["cycle_id"], at, at, score[key], unit, record)
            for key, type_, unit in RECOVERY if score.get(key) is not None]


def from_cycle(record: dict, woke: str | None) -> list[dict]:
    score = record.get("score") if record["score_state"] == "SCORED" else None
    if not score or score.get("strain") is None:
        return []
    at = woke or utc(record["start"])
    return [_sample("byobot.Strain", record["id"], at, at, score["strain"], None, record)]


def from_workout(record: dict) -> dict:
    """A performed session with the workout's totals."""
    start, end = utc(record["start"]), utc(record["end"])
    seconds = (datetime.fromisoformat(end[:-1]) - datetime.fromisoformat(start[:-1])).total_seconds()
    metrics: dict = {"duration": {"value": seconds, "unit": "s"}}
    score = (record.get("score") or {}) if record["score_state"] == "SCORED" else {}
    for key, metric, unit, scale in (("average_heart_rate", "heart_rate", "bpm", 1),
                                     ("max_heart_rate", "x.max_heart_rate", "bpm", 1),
                                     ("distance_meter", "distance", "m", 1),
                                     ("kilojoule", "x.calories", "kcal", 1 / 4.184),
                                     ("strain", "x.strain", None, 1)):
        if score.get(key) is not None:
            value = round(score[key] * scale, 1) if scale != 1 else score[key]
            metrics[metric] = {"value": value, "unit": unit} if unit else {"value": value}
    return {"id": f"whoop:workout:{record['id']}", "name": record["sport_name"].replace("_", " "),
            "started_at": start, "ended_at": end, "metrics": metrics}


def collect(client: Client, start: str) -> dict:
    """Fetch recovery, sleep, strain and workouts since `start` and map them to byobot records.

    Recovery and strain are dated when you woke, like in the WHOOP app.
    """
    sleeps = list(client.pages("/v2/activity/sleep", "PaginatedSleepResponse", start))
    recoveries = list(client.pages("/v2/recovery", "RecoveryCollection", start))
    cycles = list(client.pages("/v2/cycle", "PaginatedCycleResponse", start))
    woke = {s["id"]: utc(s["end"]) for s in sleeps}
    woke_in_cycle = {s["cycle_id"]: utc(s["end"]) for s in sleeps if not s["nap"]}
    found = [s for r in sleeps for s in from_sleep(r)]
    found += [s for r in recoveries for s in from_recovery(r, woke.get(r["sleep_id"]))]
    found += [s for r in cycles for s in from_cycle(r, woke_in_cycle.get(r["id"]))]
    samples, skipped = [], []
    for sample in found:
        errors = contract.validate("telemetry", sample)
        if errors:
            skipped.append(f"{sample['type']} at {sample['start']}: {errors[0]['message']}")
        else:
            samples.append(sample)
    sessions = [from_workout(r) for r in client.pages("/v2/activity/workout", "WorkoutCollection", start)]
    return {"samples": samples, "sessions": sessions, "skipped": skipped}
