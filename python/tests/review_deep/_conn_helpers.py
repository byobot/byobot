"""Helpers for the deep connector review (prefix `conn`). A fake WHOOP API with pages, a rate limit and a fake
clock, and WHOOP records shaped like the pinned API. Placeholders only: no real people, devices or places."""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from byobot import credentials, whoop
from byobot.commands import Context, run

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


def connect(expires_in: float = 3600) -> None:
    credentials.save(whoop.NAME, {"client_id": "example-client", "client_secret": "example-secret",
                                  "access_token": "access-1", "refresh_token": "refresh-1",
                                  "expires_at": time.time() + expires_in})


def import_whoop(monkeypatch, opener, **args) -> dict:
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    ctx = Context(actor={"kind": "human", "id": "cli"})
    try:
        return run("import whoop", ctx, args)
    finally:
        ctx.close()
