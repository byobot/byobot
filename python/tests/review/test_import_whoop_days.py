"""Review findings: which day WHOOP's records count toward in `byobot review`."""

from byobot.core import sources
from byobot.review import review

NIGHT = "00000000-0000-4000-8000-0000000000a1"
NAP = "00000000-0000-4000-8000-0000000000a2"


def scored(raw, **score):
    return {"user_id": 1, "created_at": "2026-11-20T12:00:00.000Z", "updated_at": "2026-11-20T12:00:00.000Z",
            "timezone_offset": "-05:00", "score_state": "SCORED", **raw, "score": score}


def night():
    # 22:00 on Nov 19 to 06:00 on Nov 20, local (-05:00)
    return scored({"id": NIGHT, "cycle_id": 10, "nap": False, "start": "2026-11-20T03:00:00.000Z",
                   "end": "2026-11-20T11:00:00.000Z"},
                  stage_summary={"total_light_sleep_time_milli": 14_400_000,
                                 "total_slow_wave_sleep_time_milli": 5_400_000,
                                 "total_rem_sleep_time_milli": 7_200_000},
                  sleep_performance_percentage=91.0, respiratory_rate=15.2)


def recovery():
    return scored({"cycle_id": 10, "sleep_id": NIGHT}, recovery_score=68.0, resting_heart_rate=52.0)


def test_a_nap_does_not_replace_the_nights_sleep_performance(store):
    nap = scored({"id": NAP, "cycle_id": 10, "nap": True, "start": "2026-11-20T19:00:00.000Z",
                  "end": "2026-11-20T19:40:00.000Z", "updated_at": "2026-11-20T20:00:00.000Z"},
                 stage_summary={"total_light_sleep_time_milli": 1_800_000},
                 sleep_performance_percentage=100.0, respiratory_rate=17.0)
    sources.ingest(store, "whoop", [("sleep", NIGHT, night()), ("recovery", "10", recovery()),
                                    ("sleep", NAP, nap)])
    [day] = review(store, 1, "2026-11-22")["recovery"]
    assert day["date"] == "2026-11-20"
    assert day["sleep_performance_pct"] == 91, day  # the night's, as the WHOOP app shows it
    assert day["respiratory_rate_bpm"] == 15.2, day
