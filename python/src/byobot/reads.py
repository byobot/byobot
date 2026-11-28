"""The reads behind `list`, `swap list` and `effort history`."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable

from byobot import swaps
from byobot.core.contract import instant
from byobot.core.store import Store
from byobot.util import NotFound, ValidationFailed

# kind -> (table, time column for since/until, order); a record kind, or workouts across you and your sources
LISTS = {
    "activity": ("activities", None, "name, id"),
    "location": ("locations", None, "name, id"),
    "setting": ("settings", None, "id"),
    "plan": ("plans", "starts_on", "starts_on DESC, id"),
    "planned_session": ("planned_sessions", "scheduled_on", "scheduled_on DESC, id"),
    "session": ("sessions", "start_ms", "start_ms DESC, id"),
    "effort": ("efforts", "start_ms", "start_ms DESC, id"),
    "observation": ("observations", "start_ms", "start_ms DESC, id"),
    "source_record": ("source_records", None, "revised_at DESC, id"),
    "overlay": ("overlays", None, "id"),
    "workouts": ("occurrences", "start_ms", "start_ms DESC, id"),
}
LIST_KINDS = ("events", *LISTS)
TIMED = tuple(k for k, (_, time, _) in LISTS.items() if time) + ("events",)
JSON_COLUMNS = ("tags", "equipment", "targets", "default_metrics", "resistance_curve", "value", "raw", "fields")
BRIEF_ACTIVITY = ("id", "name", "targets", "load_type", "equipment", "movement_type", "fatigue", "default_metrics",
                  "source")


def _bound(at: str) -> str | int:
    """A date (YYYY-MM-DD) bounds a local date; a timestamp bounds an instant, in epoch milliseconds."""
    try:
        if len(at) == 10:
            return date.fromisoformat(at).isoformat()
        return int(instant(at).timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        raise ValidationFailed(f"{at!r} is not a date (YYYY-MM-DD) or a timestamp") from None


def _day(at: str) -> str:
    """The date a bound names, for tables that keep dates."""
    bound = _bound(at)
    return bound if isinstance(bound, str) else at[:10]


def metrics_of(store: Store, kind: str, rid: str, part: str = "") -> dict:
    out = {}
    for r in store.rows("SELECT * FROM metrics WHERE owner_kind = ? AND owner_id = ? AND part_id = ? ORDER BY code",
                        (kind, rid, part)):
        m = {k: r[k] for k in ("value", "min", "max", "unit") if r[k] is not None}
        if r["text"] is not None:
            m["value"] = r["text"]
        if r["overlay_id"]:
            m["corrected_by"] = r["overlay_id"]
        out[r["code"]] = m
    return out


def records(store: Store, kind: str, filters: dict, detail: str, limit: int) -> dict:
    """Records of one kind, newest first; `filters` holds every filter `list` takes, None when not given."""
    sql, params, order = _select(kind, filters)
    rows = store.rows(f"{sql} ORDER BY {order} LIMIT ?", [*params, limit + 1])
    truncated = len(rows) > limit
    rows = rows[:limit]
    newer = {i for _, i in store.newer(kind)} if kind not in ("events", "workouts") else set()
    for row in rows:
        _shape(store, kind, row, detail, newer)
    return {"kind": kind, "count": len(rows), "truncated": truncated, "items": rows}


def _select(kind: str, f: dict) -> tuple[str, list, str]:
    where: list[str] = []
    params: list[Any] = []

    def add(clause: str, *values: Any) -> None:
        if values[0] is not None:
            where.append(clause)
            params.extend(values)

    sql, order = _events(f, add) if kind == "events" else _table(kind, f, add)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, params, order


def _events(f: dict, add: Callable[..., None]) -> tuple[str, str]:
    day = "substr(ts, 1, 10)"
    add("kind = ?", f["type"])
    add("id = ?", f["id"])
    instant = "CAST(round((julianday(ts) - 2440587.5) * 86400000) AS INTEGER)"  # ts keeps its writer's offset
    for value, op in ((f["since"], ">="), (f["until"], "<")):
        if value is not None:
            bound = _bound(value)
            add(f"{day if isinstance(bound, str) else instant} {op} ?", bound)
    return ("SELECT seq, id, kind, record_id, op, hlc, ts, device_id, actor_kind, actor_id, actor_model, batch, "
            "reverts, payload FROM events"), "seq DESC"


def _table(kind: str, f: dict, add: Callable[..., None]) -> tuple[str, str]:
    table, time, order = LISTS[kind]
    add("id = ?", f["id"])
    add("code = ?", f["type"] if kind == "observation" else None)
    add("source_type = ?", f["type"] if kind == "source_record" else None)
    add("activity_id = ?", f["activity"] if kind == "effort" else None)
    if kind in ("observation", "source_record", "activity"):
        add("source = ?", f["source"])
    if kind == "workouts":
        add("id IN (SELECT occurrence_id FROM occurrence_items i JOIN source_records s ON s.id = i.id "
            "WHERE s.source = ?)", f["source"])
    if kind == "activity":
        add("EXISTS (SELECT 1 FROM json_each(targets, '$.main') WHERE value = ?"
            " UNION ALL SELECT 1 FROM json_each(targets, '$.synergists') WHERE value = ?)", f["muscle"], f["muscle"])
        add("(load_type = ? OR EXISTS (SELECT 1 FROM json_each(equipment) WHERE value = ?))", f["equipment"],
            f["equipment"])
    if time is not None:
        for value, op in ((f["since"], ">="), (f["until"], "<")):
            if value is not None:
                bound = _bound(value) if time == "start_ms" else _day(value)
                add(f"{'local_date' if isinstance(bound, str) and time == 'start_ms' else time} {op} ?", bound)
    return f"SELECT * FROM {table}", order


def _shape(store: Store, kind: str, row: dict, detail: str, newer: set) -> None:
    if row.get("id") in newer:
        row["newer"] = True  # this record also holds a version a newer byobot wrote, which this one can't read
    for col in JSON_COLUMNS:
        if isinstance(row.get(col), str) and (kind != "observation" or col != "value"):
            row[col] = json.loads(row[col])
    if kind == "events":
        row["payload"] = json.loads(row["payload"])
    if kind == "activity" and detail == "brief":
        for col in [c for c in row if c not in BRIEF_ACTIVITY or row[c] is None]:
            del row[col]
    if kind in ("session", "effort"):
        row["metrics"] = metrics_of(store, kind, row["id"])
    if kind == "session":
        row["efforts"] = [e["id"] for e in store.rows(
            "SELECT id FROM efforts WHERE session_id = ? ORDER BY start_ms, id", (row["id"],))]
    if kind == "planned_session":
        _planned(store, row)
    if kind == "workouts":
        _workout(store, row)


def _planned(store: Store, row: dict) -> None:
    rid = row["id"]
    row["metrics"] = metrics_of(store, "planned_session", rid)
    row["blocks"] = store.rows("SELECT id, name, rounds FROM planned_blocks WHERE planned_session_id = ? "
                               "ORDER BY position", (rid,))
    for block in row["blocks"]:
        block["metrics"] = metrics_of(store, "planned_session", rid, block["id"])
        block["efforts"] = store.rows("SELECT id, activity_id, side FROM planned_efforts WHERE planned_session_id = ? "
                                      "AND block_id = ? ORDER BY position", (rid, block["id"]))
        for effort in block["efforts"]:
            effort["metrics"] = metrics_of(store, "planned_session", rid, effort["id"])


def _workout(store: Store, row: dict) -> None:
    oid = row.pop("id")
    row["items"] = occurrence_items(store, oid)
    row["metrics"] = {}
    for m in store.rows("SELECT * FROM occurrence_metrics WHERE occurrence_id = ? ORDER BY code", (oid,)):
        value = {k: m[k] for k in ("value", "unit") if m[k] is not None}
        if m["text"] is not None:
            value["value"] = m["text"]
        row["metrics"][m["code"]] = {**value, "from": {"kind": m["from_kind"], "id": m["from_id"]}}


def occurrence_items(store: Store, oid: str) -> list[dict]:
    """The session and source records one workout is made of, each with its source."""
    return store.rows("SELECT i.kind, i.id, s.source FROM occurrence_items i LEFT JOIN source_records s "
                      "ON i.kind = 'source_record' AND s.id = i.id WHERE occurrence_id = ? "
                      "ORDER BY i.kind, i.id", (oid,))


def _activities(store: Store) -> list[dict]:
    rows = store.rows("SELECT id, name, tags, targets, load_type, equipment FROM activities ORDER BY id")
    for row in rows:
        for col in ("tags", "targets", "equipment"):
            row[col] = json.loads(row[col]) if isinstance(row[col], str) else row[col]
    return rows


def _equipment(store: Store, location: str | None) -> list[str] | None:
    """The equipment you have at a location, else your default; None means everything counts."""
    if location is not None:
        found = store.get("location", location)
        if found is None:
            raise NotFound(f"no location {location}")
        if isinstance(found["payload"].get("equipment"), list):
            return found["payload"]["equipment"]
    rows = store.rows("SELECT value FROM settings WHERE id = 'equipment'")
    value = json.loads(rows[0]["value"]) if rows else None
    return value if isinstance(value, list) else None


def swap_list(store: Store, activity: str, location: str | None, limit: int) -> dict:
    library = _activities(store)
    original = next((a for a in library if a["id"] == activity), None)
    if original is None:
        raise NotFound(f"no activity {activity}")
    have = _equipment(store, location)
    found = swaps.alternatives(original, library, None if have is None else set(have), limit)
    items = [{**{k: a["activity"][k] for k in ("id", "name", "targets", "load_type", "equipment")
                 if a["activity"][k] is not None},
              "score": a["score"], "missing_equipment": a["missing_equipment"]} for a in found]
    return {"activity_id": activity, "location_id": location, "equipment": have, "count": len(items),
            "items": items}


def effort_history(store: Store, activity: str, location: str | None, limit: int) -> dict:
    sets = store.rows(
        "SELECT e.id, e.session_id, coalesce(e.location_id, s.location_id) AS location_id, e.performed_at,"
        " e.local_date, e.start_ms, e.side, e.planned_session_id, e.planned_effort_id FROM efforts e"
        " LEFT JOIN sessions s ON s.id = e.session_id WHERE e.activity_id = ? ORDER BY e.start_ms, e.id", (activity,))
    if not sets and not store.rows("SELECT 1 FROM activities WHERE id = ?", (activity,)):
        raise NotFound(f"no activity {activity}")
    for row in sets:
        row["metrics"] = metrics_of(store, "effort", row["id"])
    elsewhere = False
    if location is not None and any("load" in r["metrics"] for r in sets):
        here = [r for r in sets if r["location_id"] == location]
        sets, elsewhere = (here, False) if here else (sets, bool(sets))
    times: dict[str, list[dict]] = {}
    for row in sets:
        times.setdefault(row["session_id"] or row["id"], []).append(row)
    latest = sorted(times.values(), key=lambda t: (t[-1]["start_ms"], t[-1]["id"]), reverse=True)[:limit]
    return {"activity_id": activity, "location_id": location, "elsewhere": elsewhere, "times": [
        {"date": t[-1]["local_date"], "session_id": t[-1]["session_id"], "location_id": t[-1]["location_id"],
         "sets": [{k: v for k, v in r.items() if k not in ("session_id", "local_date", "start_ms") and v is not None}
                  for r in t]} for t in latest]}
