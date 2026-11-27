"""Deep review (soak): a session you logged without an end (finished on a phone that was then lost, or not finished
yet) is a point inside the WHOOP workout that covers it, and should read as that one workout. The merge rule needs a
positive overlap, so the two count as two. The seeded persona meets it: seed 4 over 200 days has 177 real workouts
and both the store and the reference model show 178, which tests/core never sees because it checks the persona's
ground truth only against the model, at seed 2 over 120 days."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _soak_helpers import V1, World  # noqa: E402

from core.scenarios import session, whoop_workout  # noqa: E402

T = "2026-03-02T{}:00.000Z".format
WID = "9a1b0000-0000-4000-8000-0000000000aa"


def test_soak_a_session_without_an_end_joins_the_workout_around_it(tmp_path):
    world = World(V1(), tmp_path)
    try:
        world.replica("laptop")
        logged = session("s1", T("10:00"), None)
        del logged["record"]["ended_at"]
        world.author("laptop", logged)
        world.ingest("laptop", "whoop", {"workout": [whoop_workout(WID, T("09:58"), T("11:00"), T("11:05"))]})
        assert len(world.occurrences("laptop")) == 1, world.occurrences("laptop")
    finally:
        world.close()
