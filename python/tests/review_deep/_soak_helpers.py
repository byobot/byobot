"""Helpers for the deep review of the core stress harness (prefix `soak`): import tests/core as the package `core`,
the name pytest gives it when it collects that directory. Placeholders only."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = str(Path(__file__).resolve().parent.parent)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

from core.drivers.cloud import Cloud  # noqa: E402
from core.drivers.v1 import V1  # noqa: E402
from core.harness import World  # noqa: E402
from core.model import ModelReplica, Refused  # noqa: E402
from core.snapshot import diff, equal  # noqa: E402

__all__ = ["Cloud", "V1", "World", "ModelReplica", "Refused", "diff", "equal", "effort"]


def effort(rid: str = "e1", reps: int = 5, notes: str | None = None) -> dict:
    record = {"id": rid, "activity_id": "row", "performed_at": "2026-03-02T10:00:00.000Z",
              "metrics": {"reps": {"value": reps}}}
    if notes:
        record["notes"] = notes
    return {"intent": "log_effort", "record": record}
