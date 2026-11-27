"""Deep review (core): an upgrade's contract change re-derives everything it reads differently."""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _core_helpers import open_store  # noqa: E402

from byobot.core import relay  # noqa: E402
from byobot.core.contract import Contract  # noqa: E402


def _grams(source_id):
    return {"source": "apple_health", "source_id": source_id, "type": "HKQuantityTypeIdentifierBodyMass",
            "value": 80000, "unit": "g", "start": "2026-03-02T07:00:00-05:00", "end": "2026-03-02T07:00:00-05:00"}


def test_a_unit_learned_on_upgrade_converts_imported_observations_too(tmp_path):
    """`use()` re-derives authored kinds when the registry's units change, but not source records, whose
    observations and workout metrics carry `std_value` just the same. They keep the old answer until a manual
    `byobot rebuild`, and every read in the code's own unit (review, the views, agents' queries) misses them."""
    path = tmp_path / "a.db"
    with open_store(path) as s:
        relay.accept_samples(s, {"samples": [_grams("g-1")], "deleted": []})
        registry = copy.deepcopy(s.contract.registry)
        assert s.rows("SELECT std_value FROM observations") == [{"std_value": None}]
    registry["codes"]["body_mass"]["units"]["g"] = 0.001  # a later release accepts grams
    upgraded = Contract(registry)
    with open_store(path, contract=upgraded) as s:  # the upgrade's first open runs use()
        after_open = s.rows("SELECT std_value FROM observations")
        s.rebuild()
        after_rebuild = s.rows("SELECT std_value FROM observations")
    assert after_rebuild == [{"std_value": 80.0}]
    assert after_open == after_rebuild, "the upgrade left the imported weight unconverted"
