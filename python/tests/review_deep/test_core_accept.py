"""Deep review (core): `Store.accept` says it never raises for bad input; one bad event in a batch must not take the
batch down with it."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _core_helpers import ME, effort, open_store, whoop_workout  # noqa: E402

from byobot.core import commands, sources  # noqa: E402
from byobot.util import ValidationFailed  # noqa: E402


def _bad(kind, store, logged, imported):
    if kind == "lone_surrogate":  # valid JSON ("\ud800"), but not encodable as UTF-8 for SQLite
        e = copy.deepcopy(logged)
        e["id"] = "019c0000-0000-7000-8000-0000000000a1"
        e["payload"]["notes"] = json.loads('"set \\ud800"')
        return e
    if kind == "bare_id_of_a_held_source_event":  # the duplicate check reads `record` before the schema check
        return {"id": imported["id"]}
    if kind == "deep_nesting":  # canonical JSON recurses in Python
        e = copy.deepcopy(logged)
        e["id"] = "019c0000-0000-7000-8000-0000000000a2"
        e["payload"]["metrics"]["x.deep"] = {"value": 1, "unit": json.loads("[" * 1500 + "]" * 1500)}
        return e
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["lone_surrogate", "bare_id_of_a_held_source_event", "deep_nesting"])
def test_a_bad_event_is_rejected_and_the_rest_of_its_batch_kept(tmp_path, kind):
    with open_store(tmp_path / "a.db") as s:
        [logged] = commands.log(s, ME, [{"kind": "effort", "payload": effort()}])
        [imported] = sources.ingest(s, "whoop", [("workout", "3001", whoop_workout(
            "3001", "2026-03-02T14:00:00.000Z", "2026-03-02T15:00:00.000Z"))])
        good = copy.deepcopy(logged)
        good.update(id="019c0000-0000-7000-8000-0000000000a3", prev=[logged["id"]], hlc="1900000000000-000000")
        good["payload"]["metrics"]["reps"]["value"] = 9
        out = s.accept_many([_bad(kind, s, logged, imported), good])
        assert out[0][0] == "rejected" and out[1] == ("accepted", "")


def test_an_agent_writing_a_lone_surrogate_gets_a_validation_error(tmp_path):
    with open_store(tmp_path / "a.db") as s:
        p = effort()
        p["notes"] = json.loads('"set \\udc00"')
        with pytest.raises(ValidationFailed):
            commands.log(s, ME, [{"kind": "effort", "payload": p}])
