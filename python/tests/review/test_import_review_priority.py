"""Review finding: `review` ignores source_priority's per-code overrides."""

from byobot.core import commands, sources
from byobot.review import review

ME = {"kind": "human", "id": "me"}
NIGHT = "00000000-0000-4000-8000-0000000000d1"


def test_a_codes_override_picks_the_daily_value_too(store):
    commands.log(store, ME, [{"kind": "setting", "id": "source_priority", "payload": {"value": {
        "default": ["whoop", "apple_health"], "codes": {"resting_heart_rate": ["apple_health", "whoop"]}}}}])
    stamp = {"user_id": 1, "created_at": "2026-11-20T12:00:00.000Z", "updated_at": "2026-11-20T12:00:00.000Z",
             "timezone_offset": "+00:00", "score_state": "SCORED"}
    sources.ingest(store, "whoop", [
        ("sleep", NIGHT, {**stamp, "id": NIGHT, "cycle_id": 10, "nap": False, "start": "2026-11-20T03:00:00.000Z",
                          "end": "2026-11-20T11:00:00.000Z", "score": {}}),
        ("recovery", "10", {**stamp, "cycle_id": 10, "sleep_id": NIGHT, "score": {"resting_heart_rate": 52.0}})])
    sources.ingest_samples(store, [{"source": "apple_health", "source_id": "rhr-1",
                                    "type": "HKQuantityTypeIdentifierRestingHeartRate", "start": "2026-11-20T12:00:00Z",
                                    "end": "2026-11-20T12:00:00Z", "value": 48, "unit": "/min"}])
    [day] = review(store, 1, "2026-11-22")["recovery"]
    assert day["resting_hr_bpm"] == 48, day  # apple_health comes first for this code
