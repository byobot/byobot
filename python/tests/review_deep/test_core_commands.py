"""Deep review (core): the authoring commands over records written elsewhere."""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _core_helpers import ME, effort, open_store  # noqa: E402

from byobot.core import commands  # noqa: E402


def test_editing_a_set_logged_with_a_code_this_registry_lacks_changes_what_was_asked(tmp_path):
    """A newer app logs a set with a registry code this install doesn't have yet (delivery keeps it). An edit of
    only `reps` here re-checks the whole payload against this registry and is refused, telling the person to use a
    registry code they never wrote; no edit or agent fix of that set works until this install upgrades."""
    with open_store(tmp_path / "a.db") as s:
        [first] = commands.log(s, ME, [{"kind": "effort", "payload": effort()}])
        newer = copy.deepcopy(first)
        newer.update(id="019c0000-0000-7000-8000-0000000000c1", prev=[first["id"]], hlc="1900000000000-000000",
                     device_id="phone")
        newer["payload"]["metrics"]["cadence"] = {"value": 30}  # a code from a later registry
        assert s.accept(newer) == ("accepted", "")
        rid = first["record"]["id"]
        commands.edit(s, ME, "effort", rid, {"metrics": {"reps": {"value": 7}}})
        payload = s.get("effort", rid)["payload"]
    assert payload["metrics"] == {"reps": {"value": 7}, "cadence": {"value": 30}}
