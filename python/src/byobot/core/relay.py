"""What a relay device, such as a phone, may write into the byobot logbook, and the plan it reads back. Every
transport (the hub, later Drive) uses these, so the two can't drift apart."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..util import ValidationFailed
from . import sources
from .contract import sync_errors
from .store import Store, busy

MAX_BATCH = 2000
PERSON = {"kind": "human", "id": "me"}  # a relay logs the person's training; it doesn't get to name another actor
NAMED_KINDS = ("location", "setting")  # kinds a phone writes under an id it picks, not a fresh UUID
CONNECTOR_KINDS = ("source_record",)  # a phone writes any other kind; one this byobot doesn't know is stored for later
OUTCOMES = ("accepted", "duplicate", "stored_unprojected", "rejected")
EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class Cut(Exception):
    """A push stopped part way because the store was busy; `results` are the events settled before it."""

    def __init__(self, results: list[dict]):
        super().__init__("the store is busy")
        self.results = results


def _needs(store: Store, event: dict) -> float:
    """The kind version an app must know to change a record on top of this event."""
    if event["op"] != "put":
        return 1
    return event["kind_version"] if store.contract.understands(event) else float("inf")


def _too_new(store: Store, event: dict, kind_versions: dict) -> bool:
    """Whether a pushed put names as prev a version its app doesn't know, whose fields the app would drop."""
    prev = event.get("prev")
    if event.get("op") != "put" or not isinstance(prev, list):
        return False
    known = kind_versions.get(event["record"].get("kind"), 1)
    held = [store.event(p) for p in prev if isinstance(p, str)]
    return any(h and h["record"] == event["record"] and _needs(store, h) > known for h in held)


def accept_events(store: Store, device_id: str, events: list, actor: dict = PERSON,
                  kind_versions: dict | None = None) -> list[dict]:
    """Accept a device's events one by one, in order. Retrying an event already accepted is harmless.

    ``device_id`` and ``actor`` are the authenticated device's. An event that names another device or actor, or
    writes a record only a connector writes, is rejected and not written. A kind this byobot doesn't know is stored,
    unprojected, for a newer one. So is a put on top of a version newer
    than ``kind_versions``, the app's kind versions (all 1 when it sends none).
    """
    results = []
    for i, event in enumerate(events):
        try:
            with store._tx():  # an event and its refusal, or the end of an earlier one, commit together
                result = _settle(store, device_id, event, actor, kind_versions or {})
                if isinstance(result["id"], str) and EVENT_ID.fullmatch(result["id"]):
                    store.note_rejections(device_id, [
                        (result["id"], result.get("reason", "") if result["status"] == "rejected" else None)])
        except sqlite3.OperationalError as exc:
            if not busy(exc):
                raise
            raise Cut(results) from exc
        results.append({"index": i, **result})
    return results


def _settle(store: Store, device_id: str, event: Any, actor: dict, kind_versions: dict) -> dict:
    result: dict[str, Any] = {"id": event.get("id") if isinstance(event, dict) else None}
    record = event.get("record") if isinstance(event, dict) else None
    if not isinstance(event, dict) or not isinstance(record, dict):
        reason = "not an event"
    elif event.get("device_id") != device_id:
        reason = "events must be authored by the paired device that pushes them"
    elif event.get("actor") != actor:
        reason = "events must be authored by the paired device's user"
    elif record.get("kind") in CONNECTOR_KINDS or event.get("op") not in ("put", "retract") or "redacted_by" in event:
        reason = "a phone writes only what a person writes"
    elif _too_new(store, event, kind_versions):
        reason = "a newer byobot wrote this record; update the app to change it"
    elif event.get("op") == "put" and store.event(event.get("id")) is None and \
            store._erase_of(record.get("kind"), record.get("id")):
        reason = "this id was erased; choose another"
    else:
        outcome, reason = store.accept(event)
        return {**result, "status": outcome, **({"reason": reason} if reason else {})}
    return {**result, "status": "rejected", "reason": reason}


def push_events(store: Store, device_id: str, body: Any, actor: dict = PERSON) -> dict:
    """An events request ({events, kind_versions}) from a device, and its answer: a result per event and the counts."""
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or len(events) > MAX_BATCH:
        raise ValidationFailed(f"send {{events: [...]}} with at most {MAX_BATCH} events")
    kind_versions = body.get("kind_versions", {})
    if sync_errors("kind_versions", kind_versions):
        raise ValidationFailed("kind_versions must map each kind to the version the app knows")
    results = accept_events(store, device_id, events, actor, kind_versions)
    return {"results": results, **{s: sum(r["status"] == s for r in results) for s in OUTCOMES}}


def accept_samples(store: Store, body: Any) -> dict:
    """A samples request ({samples, deleted}): Apple Health's samples as source records, all or none per source."""
    samples = body.get("samples", []) if isinstance(body, dict) else None
    deleted = body.get("deleted", []) if isinstance(body, dict) else None
    if not isinstance(samples, list) or not isinstance(deleted, list) or len(samples) + len(deleted) > MAX_BATCH:
        raise ValidationFailed(f"send {{samples: [...], deleted: [...]}} with at most {MAX_BATCH} in all")
    if any(not isinstance(s, dict) or s.get("source") != "apple_health" for s in samples + deleted):
        raise ValidationFailed("a phone sends only apple_health samples")
    written = sources.ingest_samples(store, samples, deleted)
    return {"written": sum(len(e) for e in written.values()),
            "by_source": {s: len(e) for s, e in written.items()}}


# -- what a relay reads --------------------------------------------------------

PLAN_SETTINGS = ("equipment", "current_location", "home_zone")
HISTORY_DAYS = 60


def _entry(store: Store, kind: str, rid: str) -> dict | None:
    """A live record as the phone keeps it: its payload, and the version a change to it names as prev. In a conflict,
    every current version too, so the phone can show them and resolve it."""
    found = store.get(kind, rid)
    if found is None:
        return None
    entry = {"id": rid, "payload": found["payload"], "prev": [found["event"]["id"]],
             "kind_version": found["event"]["kind_version"]}
    if found["conflict"]:
        heads = [store.event(h) for h in store.record(kind, rid)["heads"]]
        entry |= {"conflict": True, "versions": [_version(store, h) for h in heads if h]}
    return entry


def _version(store: Store, event: dict) -> dict:
    return {"event_id": event["id"], **({"payload": event["payload"]} if event["op"] == "put" else {}),
            "actor": event["actor"], "ts": event["ts"]}


def _entries(store: Store, kind: str, ids) -> list[dict]:
    return [e for rid in ids if (e := _entry(store, kind, rid))]


def plan_window(store: Store, start: str, end: str, device_id: str | None = None) -> dict:
    """Everything the phone needs to run the sessions scheduled from ``start`` to ``end`` (inclusive).

    Activities (yours, and the library's that the rest names, with an empty prev; the app ships the library),
    locations, the settings a workout reads, plans, planned sessions, and what was logged: the sessions against the
    planned ones, those of the ``HISTORY_DAYS`` before ``start`` through ``end``, and those holding the last two
    times each activity was done at each location, all with their efforts, and efforts logged outside a session in
    those days.

    Beside them: the removed records a phone would name by an id it picks, so it picks another or names the removal
    as prev; this store's id and clock, the erased records a phone may hold, its kind versions, and for
    ``device_id`` the latest of its events held and the ones refused.
    """
    with store.snapshot():
        return _plan_window(store, start, end, device_id)


def _plan_window(store: Store, start: str, end: str, device_id: str | None) -> dict:
    ids = lambda sql, *p: [r[0] for r in store.conn.execute(sql, p)]
    planned = ids("SELECT id FROM planned_sessions WHERE scheduled_on >= ? AND scheduled_on <= ? "
                  "ORDER BY scheduled_on, name, id", start, end)
    plans = ids("SELECT id FROM plans WHERE id IN (SELECT plan_id FROM planned_sessions WHERE id IN "
                "(SELECT value FROM json_each(?))) OR (starts_on <= ? AND (ends_on IS NULL OR ends_on >= ?)) "
                "ORDER BY starts_on, id", json.dumps(planned), end, start)
    since = _since(start)
    window, recent = json.dumps(planned), json.dumps(_recent_efforts(store))
    sessions = ids("SELECT id FROM sessions WHERE planned_session_id IN (SELECT value FROM json_each(?)) "
                   "OR (local_date >= ? AND local_date <= ?) "
                   "OR id IN (SELECT session_id FROM efforts WHERE id IN (SELECT value FROM json_each(?))) "
                   "ORDER BY start_ms, id", window, since, end, recent)
    efforts = ids("SELECT id FROM efforts WHERE planned_session_id IN (SELECT value FROM json_each(?)) "
                  "OR session_id IN (SELECT value FROM json_each(?)) "
                  "OR (session_id IS NULL AND local_date >= ? AND local_date <= ?) "
                  "OR id IN (SELECT value FROM json_each(?)) ORDER BY start_ms, id",
                  window, json.dumps(sessions), since, end, recent)
    activities = []
    for r in store.rows("SELECT id, source FROM activities WHERE source = 'store' OR id IN (SELECT activity_id FROM "
                        "planned_efforts WHERE planned_session_id IN (SELECT value FROM json_each(?)) UNION SELECT "
                        "activity_id FROM efforts WHERE id IN (SELECT value FROM json_each(?))) ORDER BY id",
                        (window, json.dumps(efforts))):
        if r["source"] == "store":
            activities += _entries(store, "activity", [r["id"]])
        else:
            row = store.rows("SELECT * FROM activities WHERE id = ?", (r["id"],))[0]
            payload = {k: json.loads(row[k]) if k in _JSON else row[k] for k in _ACTIVITY if row[k] is not None}
            activities.append({"id": r["id"], "payload": payload, "prev": [],
                               "kind_version": store.contract.version("activity"), "library": True})
    return {"from": start, "to": end, "since": since, "activities": activities,
            "locations": _entries(store, "location", ids("SELECT id FROM locations ORDER BY name, id")),
            "settings": _entries(store, "setting", PLAN_SETTINGS), "plans": _entries(store, "plan", plans),
            "planned_sessions": _entries(store, "planned_session", planned),
            "sessions": _entries(store, "session", sessions),
            "efforts": _entries(store, "effort", efforts), "removed": _removed(store),
            **_device_state(store, device_id)}


def _removed(store: Store) -> list[dict]:
    rows = store.conn.execute("SELECT kind, id, heads FROM records WHERE live = 0 AND erased = 0 AND heads != '[]' "
                              "AND kind IN (SELECT value FROM json_each(?)) ORDER BY kind, id",
                              (json.dumps(NAMED_KINDS),))
    return [{"kind": r["kind"], "id": r["id"], "prev": json.loads(r["heads"])} for r in rows]


def _since(start: str) -> str:
    """The first day of logged history the plan carries."""
    try:
        return (date.fromisoformat(start) - timedelta(days=HISTORY_DAYS)).isoformat()
    except ValueError:
        return start


def _device_state(store: Store, device_id: str | None) -> dict:
    received = store.received(device_id) if device_id else None
    erased = [{"kind": k, "id": i} for k, i in store.erased() if k not in CONNECTOR_KINDS]
    return {"store_id": store.store_id, "hlc": store.hlc, "erased": erased,
            "kind_versions": store.contract.kind_versions(),
            "received": {device_id: received} if received else {},
            "rejected": store.rejections(device_id) if device_id else []}


_ACTIVITY = ("name", "movement_type", "fatigue", "tags", "load_type", "equipment", "targets", "default_metrics",
             "resistance_curve")
_JSON = ("tags", "equipment", "targets", "default_metrics", "resistance_curve")


def _recent_efforts(store: Store, times: int = 2) -> list[str]:
    """Efforts from the last `times` occasions each activity was done, per location."""
    rows = store.conn.execute("""
        WITH done AS (
          SELECT e.id, e.activity_id, e.start_ms, coalesce(e.location_id, s.location_id, '') AS location_id,
                 coalesce(e.session_id, e.local_date) AS occasion
          FROM efforts e LEFT JOIN sessions s ON s.id = e.session_id),
        occasions AS (
          SELECT activity_id, location_id, occasion, dense_rank() OVER (
            PARTITION BY activity_id, location_id ORDER BY max(start_ms) DESC, occasion DESC) AS n
          FROM done GROUP BY activity_id, location_id, occasion)
        SELECT d.id FROM done d JOIN occasions o ON o.activity_id = d.activity_id AND o.occasion = d.occasion
          AND o.location_id = d.location_id
        WHERE o.n <= ? ORDER BY d.start_ms, d.id""", (times,))
    return [r[0] for r in rows]
