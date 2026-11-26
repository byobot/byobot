"""What a relay device, such as a phone, may write into the byobot logbook, and the plan it reads back. Every
transport (the hub, later Drive) uses these, so the two can't drift apart."""

from __future__ import annotations

import json
from typing import Any

from ..util import ValidationFailed
from . import reducers, sources
from .commands import AUTHORED
from .contract import sync_errors
from .store import Store

MAX_BATCH = 2000
PERSON = {"kind": "human", "id": "me"}  # a relay logs the person's training; it doesn't get to name another actor
KINDS = (*AUTHORED, "overlay")
OUTCOMES = ("accepted", "duplicate", "stored_unprojected", "rejected")


def _needs(store: Store, event: dict) -> float:
    """The kind version an app must know to change a record on top of this event."""
    if event["op"] != "put":
        return 1
    if not store.contract.understands(event):
        return float("inf")
    kind = event["record"]["kind"]
    return reducers.lowest(store.contract, kind, reducers.upcast(store.contract, kind, event.get("kind_version"),
                                                                 event["payload"]))


def _too_new(store: Store, event: dict, kinds: dict) -> bool:
    """Whether a pushed put names as prev a version its app doesn't know, whose fields the app would drop."""
    prev = event.get("prev")
    if event.get("op") != "put" or not isinstance(prev, list):
        return False
    known = kinds.get(event["record"].get("kind"), 1)
    held = [store.event(p) for p in prev if isinstance(p, str)]
    return any(h and h["record"] == event["record"] and _needs(store, h) > known for h in held)


def accept_events(store: Store, device_id: str, events: list, actor: dict = PERSON,
                  kinds: dict | None = None) -> list[dict]:
    """Accept a device's events one by one, in order. Retrying an event already accepted is harmless.

    ``device_id`` and ``actor`` are the authenticated device's. An event that names another device or actor, or
    writes a record a person doesn't write, is rejected and not written. So is a put on top of a version newer
    than ``kinds``, the app's kind versions (all 1 when it sends none).
    """
    results = []
    for i, event in enumerate(events):
        result: dict[str, Any] = {"index": i, "id": event.get("id") if isinstance(event, dict) else None}
        record = event.get("record") if isinstance(event, dict) else None
        if not isinstance(event, dict) or not isinstance(record, dict):
            reason = "not an event"
        elif event.get("device_id") != device_id:
            reason = "events must be authored by the paired device that pushes them"
        elif event.get("actor") != actor:
            reason = "events must be authored by the paired device's user"
        elif record.get("kind") not in KINDS or event.get("op") not in ("put", "retract") or "redacted_by" in event:
            reason = "a phone writes only what a person writes"
        elif _too_new(store, event, kinds or {}):
            reason = "a newer byobot wrote this record; update the app to change it"
        else:
            outcome, reason = store.accept(event)
            results.append({**result, "status": outcome, **({"reason": reason} if reason else {})})
            continue
        results.append({**result, "status": "rejected", "reason": reason})
    return results


def push_events(store: Store, device_id: str, body: Any, actor: dict = PERSON) -> dict:
    """An events request ({events, kinds}) from a device, and its answer: a result per event and the counts."""
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or len(events) > MAX_BATCH:
        raise ValidationFailed(f"send {{events: [...]}} with at most {MAX_BATCH} events")
    kinds = body.get("kinds", {})
    if sync_errors("kinds", kinds):
        raise ValidationFailed("kinds must map each kind to the version the app knows")
    results = accept_events(store, device_id, events, actor, kinds)
    return {"results": results, **{s: sum(r["status"] == s for r in results) for s in OUTCOMES}}


def accept_samples(store: Store, body: Any) -> dict:
    """A samples request ({samples, deleted}): Apple Health's samples as source records, all or none per source."""
    samples = body.get("samples", []) if isinstance(body, dict) else None
    deleted = body.get("deleted", []) if isinstance(body, dict) else None
    if not isinstance(samples, list) or not isinstance(deleted, list) or len(samples) + len(deleted) > MAX_BATCH:
        raise ValidationFailed(f"send {{samples: [...], deleted: [...]}} with at most {MAX_BATCH} in all")
    if any(not isinstance(s, dict) or sources.SAMPLE_SOURCES.get(s.get("source"), s.get("source")) != "apple_health"
           for s in samples + deleted):
        raise ValidationFailed("a phone sends only apple_health samples")
    written = sources.ingest_samples(store, samples, deleted)
    return {"written": sum(len(e) for e in written.values()),
            "by_source": {s: len(e) for s, e in written.items()}}


# -- what a relay reads --------------------------------------------------------

PLAN_SETTINGS = ("equipment", "current_location", "home_zone")


def _entry(store: Store, kind: str, rid: str) -> dict | None:
    """A live record as the phone keeps it: its payload, and the version a change to it names as prev."""
    found = store.get(kind, rid)
    if found is None:
        return None
    return {"id": rid, "payload": found["payload"], "prev": [found["event"]["id"]],
            "kind_version": reducers.lowest(store.contract, kind, found["payload"]),
            **({"conflict": True} if found["conflict"] else {})}


def _entries(store: Store, kind: str, ids) -> list[dict]:
    return [e for rid in ids if (e := _entry(store, kind, rid))]


def plan_window(store: Store, start: str, end: str) -> dict:
    """Everything the phone needs to run the sessions scheduled from ``start`` to ``end`` (inclusive).

    Activities (yours, and the library's with an empty prev), locations, the settings a workout reads, plans,
    planned sessions and their rearrangements, the sessions and efforts already logged against them, and the
    efforts of the last two times each planned activity was done at each location.
    """
    ids = lambda sql, *p: [r[0] for r in store.conn.execute(sql, p)]
    activities = []
    for r in store.rows("SELECT id, source FROM activities ORDER BY id"):
        if r["source"] == "store":
            activities += _entries(store, "activity", [r["id"]])
        else:
            row = store.rows("SELECT * FROM activities WHERE id = ?", (r["id"],))[0]
            payload = {k: json.loads(row[k]) if k in _JSON else row[k] for k in _ACTIVITY if row[k] is not None}
            activities.append({"id": r["id"], "payload": payload, "prev": [],
                               "kind_version": reducers.lowest(store.contract, "activity", payload), "library": True})
    planned = ids("SELECT id FROM planned_sessions WHERE scheduled_on >= ? AND scheduled_on <= ? "
                  "ORDER BY scheduled_on, name, id", start, end)
    plans = ids("SELECT id FROM plans WHERE id IN (SELECT plan_id FROM planned_sessions WHERE id IN "
                "(SELECT value FROM json_each(?))) OR (starts_on <= ? AND (ends_on IS NULL OR ends_on >= ?)) "
                "ORDER BY starts_on, id", json.dumps(planned), end, start)
    window = json.dumps(planned)
    sessions = ids("SELECT id FROM sessions WHERE planned_session_id IN (SELECT value FROM json_each(?)) "
                   "ORDER BY start_ms, id", window)
    efforts = ids("SELECT id FROM efforts WHERE planned_session_id IN (SELECT value FROM json_each(?)) "
                  "OR session_id IN (SELECT value FROM json_each(?)) ORDER BY start_ms, id", window,
                  json.dumps(sessions))
    efforts += [e for e in _recent_efforts(store, planned) if e not in efforts]
    overlays = ids("SELECT id FROM overlays WHERE effect = 'rearrange' AND target_kind = 'planned_session' "
                   "AND target_id IN (SELECT value FROM json_each(?)) ORDER BY id", window)
    return {"from": start, "to": end, "activities": activities,
            "locations": _entries(store, "location", ids("SELECT id FROM locations ORDER BY name, id")),
            "settings": _entries(store, "setting", PLAN_SETTINGS), "plans": _entries(store, "plan", plans),
            "planned_sessions": _entries(store, "planned_session", planned),
            "overlays": _entries(store, "overlay", overlays), "sessions": _entries(store, "session", sessions),
            "efforts": _entries(store, "effort", efforts)}


_ACTIVITY = ("name", "movement_type", "fatigue", "tags", "equipment", "targets", "default_metrics",
             "resistance_curve")
_JSON = ("tags", "equipment", "targets", "default_metrics", "resistance_curve")


def _recent_efforts(store: Store, planned: list[str], times: int = 2) -> list[str]:
    """Efforts from the last `times` occasions each planned activity was done, per location."""
    rows = store.conn.execute("""
        WITH wanted AS (
          SELECT DISTINCT activity_id FROM planned_efforts WHERE planned_session_id IN (SELECT value FROM json_each(?))),
        done AS (
          SELECT e.id, e.activity_id, e.start_ms, coalesce(e.location_id, s.location_id, '') AS location_id,
                 coalesce(e.session_id, e.local_date) AS occasion
          FROM efforts e LEFT JOIN sessions s ON s.id = e.session_id
          WHERE e.activity_id IN (SELECT activity_id FROM wanted)),
        occasions AS (
          SELECT activity_id, location_id, occasion, dense_rank() OVER (
            PARTITION BY activity_id, location_id ORDER BY max(start_ms) DESC, occasion DESC) AS n
          FROM done GROUP BY activity_id, location_id, occasion)
        SELECT d.id FROM done d JOIN occasions o ON o.activity_id = d.activity_id AND o.occasion = d.occasion
          AND o.location_id = d.location_id
        WHERE o.n <= ? ORDER BY d.start_ms, d.id""", (json.dumps(planned), times))
    return [r[0] for r in rows]
