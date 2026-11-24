"""Import from Open Wearables, which connects WHOOP and other wearables.

byobot reads Open Wearables' normalized API and writes telemetry samples keyed on
(provider, type and start), so re-importing changes nothing even if Open Wearables
is reinstalled. Every response is checked against the API of the pinned Open
Wearables release before anything is written.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any, Callable, Iterator

from jsonschema import Draft202012Validator

from byobot.util import ByobotError

SNAPSHOT = Path(__file__).with_name("open_wearables.schema.json")
PAGE = 1000

# Open Wearables series type -> (byobot type, the unit Open Wearables reports, byobot unit)
SERIES = {
    "resting_heart_rate": ("HKQuantityTypeIdentifierRestingHeartRate", "bpm", "count/min"),
    "heart_rate_variability_rmssd": ("byobot.HeartRateVariabilityRMSSD", "ms", "ms"),
    "heart_rate_variability_sdnn": ("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "ms", "ms"),
    "oxygen_saturation": ("HKQuantityTypeIdentifierOxygenSaturation", "percent", "%"),
    "respiratory_rate": ("HKQuantityTypeIdentifierRespiratoryRate", "brpm", "count/min"),
    "skin_temperature": ("byobot.SkinTemperature", "celsius", "degC"),
}
STAGES = {
    "light_minutes": "byobot.SleepLightDuration",
    "deep_minutes": "byobot.SleepDeepDuration",
    "rem_minutes": "byobot.SleepREMDuration",
    "awake_minutes": "byobot.SleepAwakeDuration",
}
SCORES = {
    "recovery": ("byobot.RecoveryScore", "%"),
    "sleep": ("byobot.SleepPerformance", "%"),
    "strain": ("byobot.Strain", None),
}
BOUNDS = {"%": (0, 100), None: (0, 21)}


class UpstreamError(ByobotError):
    code = "upstream"


def version() -> str:
    return _snapshot()["x-open-wearables-version"]


@cache
def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text())


def check(response: Any, name: str) -> None:
    """Refuse a response that doesn't match the pinned Open Wearables API."""
    schema = {**_snapshot(), "$ref": f"#/components/schemas/PaginatedResponse_{name}_"}
    errors = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise UpstreamError(
            f"Open Wearables sent a {name} response byobot doesn't recognize (at {where}: {first.message}). "
            f"byobot reads the API of Open Wearables {version()}.")


class Client:
    def __init__(self, url: str, user_id: str, api_key: str,
                 opener: Callable[[urllib.request.Request], Any] = urllib.request.urlopen):
        self.base = url.rstrip("/")
        self.user_id = user_id
        self.api_key = api_key
        self.opener = opener

    def get(self, path: str, params: dict) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        user = urllib.parse.quote(self.user_id, safe="")
        url = f"{self.base}/api/v1/users/{user}/{path}?{query}"
        req = urllib.request.Request(url, headers={"X-Open-Wearables-API-Key": self.api_key,
                                                   "Accept": "application/json"})
        try:
            with self.opener(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise UpstreamError(f"Open Wearables answered {e.code} for {path}") from None
        except (urllib.error.URLError, OSError) as e:
            raise UpstreamError(f"can't reach Open Wearables at {self.base}: {getattr(e, 'reason', e)}") from None
        except json.JSONDecodeError:
            raise UpstreamError(f"Open Wearables sent something other than JSON for {path}") from None

    def pages(self, path: str, name: str, params: dict) -> Iterator[dict]:
        """Every record of a paginated endpoint. Health scores page by offset, the rest by cursor."""
        params = {**params, "limit": PAGE}
        offset = 0
        while True:
            body = self.get(path, params)
            check(body, name)
            yield from body["data"]
            more = body["pagination"].get("has_more")
            if name == "HealthScoreResponse":
                offset += len(body["data"])
                if not more or not body["data"]:
                    return
                params["offset"] = offset
            else:
                cursor = body["pagination"].get("next_cursor")
                if not more or not cursor:
                    return
                params["cursor"] = cursor


def utc(ts: str) -> str:
    """An RFC 3339 instant in UTC. A time with no offset is taken as UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sample(source: dict | None, type_: str, start: str, end: str, value: Any, unit: str | None, raw: dict) -> dict:
    source = source or {}
    sample = {"source": source.get("provider") or "open-wearables", "source_id": f"{type_}:{start}",
              "type": type_, "start": start, "end": end, "value": value, "raw": raw}
    if unit:
        sample["unit"] = unit
    device = {k: v for k, v in (("id", source.get("device_id") or source.get("device_name")),
                                ("name", source.get("device_name")), ("model", source.get("device"))) if v}
    if "id" in device:
        sample["device"] = device
    return sample


def from_series(record: dict) -> dict | str:
    """A telemetry sample, or why the record was skipped."""
    type_, expected, unit = SERIES[record["type"]]
    if record["unit"] != expected:
        raise UpstreamError(f"Open Wearables reports {record['type']} in {record['unit']!r}; byobot expects {expected!r}")
    value = record["value"]
    if record["type"] == "oxygen_saturation" and value <= 1:
        return "oxygen saturation looks like a fraction, not a percentage"
    at = utc(record["timestamp"])
    end = utc(record["period_end"]) if record.get("period_end") else at
    return _sample(record.get("source"), type_, at, end, value, unit, record)


def from_sleep(record: dict) -> list[dict]:
    start, end = utc(record["start_time"]), utc(record["end_time"])
    samples = [_sample(record["source"], "HKCategoryTypeIdentifierSleepAnalysis", start, end, "inBed", None, record)]
    for key, type_ in STAGES.items():
        minutes = (record.get("stages") or {}).get(key)
        if minutes is not None:
            samples.append(_sample(record["source"], type_, start, end, minutes * 60, "s", record))
    return samples


def from_score(record: dict) -> list[dict] | str:
    if record["category"] not in SCORES or record.get("value") is None:
        return []
    type_, unit = SCORES[record["category"]]
    low, high = BOUNDS[unit]
    if not low <= record["value"] <= high:
        return f"{record['category']} score {record['value']} is outside {low} to {high}"
    at = utc(record["recorded_at"])
    source = {**(record.get("source") or {}), "provider": record["provider"]}
    samples = [_sample(source, type_, at, at, record["value"], unit, record)]
    rate = ((record.get("components") or {}).get("respiratory_rate") or {}).get("value")
    if record["category"] == "sleep" and rate:
        samples.append(_sample(source, "HKQuantityTypeIdentifierRespiratoryRate", at, at, rate, "count/min", record))
    return samples


def collect(client: Client, start: str, end: str, provider: str | None = None) -> dict:
    """Fetch recovery, sleep and score records between two dates and map them to telemetry samples."""
    window = {"provider": provider}
    samples: list[dict] = []
    skipped: list[str] = []

    def add(result: Any) -> None:
        if isinstance(result, str):
            skipped.append(result)
        else:
            samples.extend(result if isinstance(result, list) else [result])

    for record in client.pages("timeseries", "TimeSeriesSample",
                               {**window, "start_time": start, "end_time": end, "types": list(SERIES)}):
        if record["type"] in SERIES:
            add(from_series(record))
    for record in client.pages("events/sleep", "SleepSession", {**window, "start_date": start, "end_date": end}):
        add(from_sleep(record))
    for record in client.pages("health-scores", "HealthScoreResponse",
                               {**window, "start_date": start, "end_date": end}):
        add(from_score(record))
    return {"samples": samples, "skipped": skipped}
