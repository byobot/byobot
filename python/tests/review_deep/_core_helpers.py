"""Helpers for the deep core correctness review (prefix `core`). Placeholders only: no real people or places."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from byobot.core.store import Store

ME = {"kind": "human", "id": "me"}
AGENT = {"kind": "agent", "id": "example-agent"}
EST = timezone(timedelta(hours=-5))

DERIVED = ("activities", "locations", "settings", "plans", "planned_sessions", "planned_blocks", "planned_efforts",
           "sessions", "efforts", "observations", "metrics", "source_records", "source_links", "source_sessions",
           "overlays", "occurrences", "occurrence_items", "occurrence_metrics")


class Wall:
    """A wall clock that moves one second per reading, unless told otherwise."""

    def __init__(self, start=datetime(2026, 3, 2, 15, tzinfo=timezone.utc), step=timedelta(seconds=1)):
        self.at, self.step = start, step

    def __call__(self):
        self.at += self.step
        return self.at


def open_store(path, device_id="laptop", wall=None, **kw) -> Store:
    return Store(path, device_id=device_id, wall=wall or Wall(), **kw)


def replica_of(store: Store, path, device_id="phone", wall=None) -> Store:
    other = open_store(path, device_id=device_id, wall=wall)
    other.adopt(store.store_id, store._get("store_key"))
    return other


def session(start="2026-03-02T09:00:00-05:00", end="2026-03-02T10:00:00-05:00", name="lift", **more) -> dict:
    return {"name": name, "time": {"start": start, "end": end}, "modality": "self_reported", **more}


def effort(sid=None, reps=5, start="2026-03-02T09:10:00-05:00") -> dict:
    p = {"activity_id": "row", "time": {"start": start}, "modality": "self_reported",
         "metrics": {"reps": {"value": reps}}}
    if sid:
        p["links"] = {"part_of": {"kind": "session", "id": sid}}
    return p


def whoop_workout(wid: str, start: str, end: str, updated="2026-03-02T16:00:00.000Z", strain=10.0, hr=140,
                  offset="-05:00") -> dict:
    return {"id": wid, "start": start, "end": end, "timezone_offset": offset, "updated_at": updated,
            "sport_name": "running", "score_state": "SCORED",
            "score": {"strain": strain, "average_heart_rate": hr, "max_heart_rate": hr + 30}}


def derived(store: Store) -> dict:
    """Every derived table's rows, sorted, so two stores (or one before and after a rebuild) compare exactly."""
    out = {}
    for table in DERIVED:
        rows = [json.dumps(dict(r), sort_keys=True) for r in store.conn.execute(f"SELECT * FROM {table}")]
        out[table] = sorted(rows)
    out["records"] = sorted(json.dumps(dict(r), sort_keys=True) for r in store.conn.execute("SELECT * FROM records"))
    return out


def differences(a: dict, b: dict) -> list[str]:
    out = []
    for table in sorted(set(a) | set(b)):
        x, y = set(a.get(table, [])), set(b.get(table, []))
        for row in sorted(x - y):
            out.append(f"{table}: only before rebuild: {row}")
        for row in sorted(y - x):
            out.append(f"{table}: only after rebuild: {row}")
    return out
