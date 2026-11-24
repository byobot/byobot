import copy
import io
import json
import urllib.parse

import pytest

from byobot import open_wearables
from byobot.commands import Context, run
from byobot.util import ValidationFailed

SOURCE = {"provider": "whoop", "source": None, "device": None, "device_type": None, "device_id": None,
          "device_name": None}
PAGE_END = {"next_cursor": None, "previous_cursor": None, "has_more": False, "total_count": None}

SERIES = [
    {"timestamp": "2026-11-20T11:00:00Z", "zone_offset": "-05:00", "type": "resting_heart_rate", "value": 52,
     "unit": "bpm", "source": SOURCE},
    {"timestamp": "2026-11-20T11:00:00Z", "zone_offset": "-05:00", "type": "heart_rate_variability_rmssd",
     "value": 61.2, "unit": "ms", "source": SOURCE},
    {"timestamp": "2026-11-20T11:00:00Z", "zone_offset": "-05:00", "type": "oxygen_saturation", "value": 96.5,
     "unit": "percent", "source": SOURCE},
    {"timestamp": "2026-11-20T11:00:00Z", "zone_offset": "-05:00", "type": "skin_temperature", "value": 33.4,
     "unit": "celsius", "source": SOURCE},
]
SLEEP = [{
    "id": "00000000-0000-4000-8000-000000000001", "start_time": "2026-11-20T03:00:00Z",
    "end_time": "2026-11-20T11:00:00Z", "zone_offset": "-05:00", "source": SOURCE, "duration_seconds": 28800,
    "sleep_duration_seconds": 25200, "time_in_bed_seconds": 28800, "efficiency_percent": 90.0,
    "stages": {"awake_minutes": 30, "light_minutes": 240, "deep_minutes": 90, "rem_minutes": 105},
    "sleep_stage_intervals": None, "is_nap": False, "is_final": True,
}]
SCORES = [
    {"category": "recovery", "value": 68, "qualifier": None, "recorded_at": "2026-11-20T11:05:00Z",
     "zone_offset": None, "components": {"resting_heart_rate": {"value": 52, "qualifier": None}},
     "id": "00000000-0000-4000-8000-000000000002", "data_source_id": None, "provider": "whoop",
     "event_record_id": None, "source": None},
    {"category": "sleep", "value": 88, "qualifier": None, "recorded_at": "2026-11-20T03:00:00Z",
     "zone_offset": None, "components": {"respiratory_rate": {"value": 15.2, "qualifier": None}},
     "id": "00000000-0000-4000-8000-000000000003", "data_source_id": None, "provider": "whoop",
     "event_record_id": None, "source": None},
    {"category": "strain", "value": 12.7, "qualifier": None, "recorded_at": "2026-11-20T05:00:00Z",
     "zone_offset": "-05:00", "components": None, "id": "00000000-0000-4000-8000-000000000004",
     "data_source_id": None, "provider": "whoop", "event_record_id": None, "source": None},
    {"category": "resilience", "value": 70, "qualifier": None, "recorded_at": "2026-11-20T05:00:00Z",
     "zone_offset": None, "components": None, "id": "00000000-0000-4000-8000-000000000005",
     "data_source_id": None, "provider": "whoop", "event_record_id": None, "source": None},
]


def page(data, pagination=PAGE_END):
    return {"data": data, "pagination": pagination, "metadata": {}}


class Fake:
    """Answers like Open Wearables, from canned pages per path."""

    def __init__(self, pages):
        self.pages = {k: list(v) for k, v in pages.items()}
        self.requests = []

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        self.requests.append((url.path, urllib.parse.parse_qs(url.query), dict(req.header_items())))
        path = url.path.split("/users/", 1)[1].split("/", 1)[1]
        return io.BytesIO(json.dumps(self.pages[path].pop(0)).encode())


def responses(series=SERIES, sleep=SLEEP, scores=SCORES):
    return {"timeseries": [page(series)], "events/sleep": [page(sleep)], "health-scores": [page(scores)]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPEN_WEARABLES_API_KEY", "example-key")
    monkeypatch.setenv("OPEN_WEARABLES_USER_ID", "example-user")


REAL_INIT = open_wearables.Client.__init__


def importer(monkeypatch, fake):
    def init(self, url, user_id, api_key, opener=None):
        REAL_INIT(self, url, user_id, api_key, fake)

    monkeypatch.setattr(open_wearables.Client, "__init__", init)
    ctx = Context(actor={"kind": "human", "id": "cli"})
    try:
        return run("import-open-wearables", ctx, {})
    finally:
        ctx.close()


def test_snapshot_pins_a_version_and_covers_every_response():
    snap = json.loads(open_wearables.SNAPSHOT.read_text())
    assert snap["x-open-wearables-version"] == "0.11.0"
    for name in ("TimeSeriesSample", "SleepSession", "HealthScoreResponse", "Workout"):
        assert f"PaginatedResponse_{name}_" in snap["components"]["schemas"]


@pytest.mark.parametrize("name,data", [("TimeSeriesSample", SERIES), ("SleepSession", SLEEP),
                                       ("HealthScoreResponse", SCORES)])
def test_example_responses_match_the_pinned_api(name, data):
    open_wearables.check(page(data), name)


def test_import_maps_recovery_sleep_and_scores(monkeypatch, env, store):
    out = importer(monkeypatch, Fake(responses()))
    assert out["telemetry"] == {"inserted": 13, "updated": 0, "unchanged": 0}
    assert out["skipped"] == [] and out["open_wearables_api"] == "0.11.0"
    rows = {r["type"]: r for r in store.rows("SELECT * FROM telemetry_samples")}
    assert {r["source"] for r in rows.values()} == {"whoop"}
    assert rows["HKQuantityTypeIdentifierRestingHeartRate"]["value"] == 52
    assert rows["HKQuantityTypeIdentifierRestingHeartRate"]["unit"] == "count/min"
    assert rows["HKQuantityTypeIdentifierOxygenSaturation"]["unit"] == "%"
    assert rows["byobot.SkinTemperature"]["unit"] == "degC"
    assert rows["HKCategoryTypeIdentifierSleepAnalysis"]["category"] == "inBed"
    assert rows["HKCategoryTypeIdentifierSleepAnalysis"]["end_at"] == "2026-11-20T11:00:00Z"
    assert rows["byobot.SleepDeepDuration"]["value"] == 5400
    assert rows["byobot.RecoveryScore"]["value"] == 68
    assert rows["byobot.SleepPerformance"]["value"] == 88
    assert rows["HKQuantityTypeIdentifierRespiratoryRate"]["value"] == 15.2
    assert rows["byobot.Strain"]["value"] == 12.7 and rows["byobot.Strain"]["unit"] is None
    assert rows["byobot.RecoveryScore"]["source_id"] == "byobot.RecoveryScore:2026-11-20T11:05:00Z"


def test_reimport_changes_nothing(monkeypatch, env, store):
    importer(monkeypatch, Fake(responses()))
    out = importer(monkeypatch, Fake(responses()))
    assert out["telemetry"] == {"inserted": 0, "updated": 0, "unchanged": 13}


def test_a_rescored_value_updates_in_place(monkeypatch, env, store):
    importer(monkeypatch, Fake(responses()))
    rescored = copy.deepcopy(SCORES)
    rescored[0]["value"] = 71
    out = importer(monkeypatch, Fake(responses(scores=rescored)))
    assert out["telemetry"]["updated"] == 1
    assert store.rows("SELECT value FROM telemetry_samples WHERE type = 'byobot.RecoveryScore'")[0]["value"] == 71


def test_offsets_are_normalized_to_utc():
    shifted = {**SERIES[0], "timestamp": "2026-11-20T06:00:00-05:00"}
    assert open_wearables.from_series(shifted)["start"] == "2026-11-20T11:00:00Z"
    naive = {**SERIES[0], "timestamp": "2026-11-20T11:00:00"}
    assert open_wearables.from_series(naive)["source_id"].endswith(":2026-11-20T11:00:00Z")


def test_an_unrecognized_response_is_refused_before_anything_is_written(monkeypatch, env, store):
    drifted = copy.deepcopy(SLEEP)
    drifted[0]["begin_time"] = drifted[0].pop("start_time")
    with pytest.raises(open_wearables.UpstreamError, match="0.11.0"):
        importer(monkeypatch, Fake(responses(sleep=drifted)))
    assert store.rows("SELECT count(*) AS n FROM telemetry_samples")[0]["n"] == 0


def test_a_changed_unit_is_refused(monkeypatch, env, store):
    grams = [{**SERIES[0], "unit": "beats"}]
    with pytest.raises(open_wearables.UpstreamError, match="resting_heart_rate"):
        importer(monkeypatch, Fake(responses(series=grams)))


def test_out_of_range_values_are_skipped_and_reported(monkeypatch, env, store):
    odd = [{**SERIES[2], "value": 0.965}]
    scores = [{**SCORES[0], "value": 140}]
    out = importer(monkeypatch, Fake(responses(series=odd, scores=scores)))
    assert len(out["skipped"]) == 2


def test_pages_are_followed(monkeypatch, env, store):
    more = {**PAGE_END, "has_more": True, "next_cursor": "c2"}
    fake = Fake({"timeseries": [page(SERIES[:2], more), page(SERIES[2:])],
                 "events/sleep": [page([])], "health-scores": [page([])]})
    out = importer(monkeypatch, fake)
    assert out["telemetry"]["inserted"] == 4
    series = [r for r in fake.requests if r[0].endswith("/timeseries")]
    assert series[1][1]["cursor"] == ["c2"]


def test_requests_carry_the_key_and_the_user(monkeypatch, env, store):
    fake = Fake(responses())
    importer(monkeypatch, fake)
    path, query, headers = fake.requests[0]
    assert path == "/api/v1/users/example-user/timeseries"
    assert headers["X-open-wearables-api-key"] == "example-key"
    assert set(query["types"]) == set(open_wearables.SERIES)


def test_the_key_is_required(monkeypatch, store):
    monkeypatch.delenv("OPEN_WEARABLES_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_WEARABLES_USER_ID", "example-user")
    with pytest.raises(ValidationFailed, match="OPEN_WEARABLES_API_KEY"):
        run("import-open-wearables", Context(actor={"kind": "human", "id": "cli"}), {})
