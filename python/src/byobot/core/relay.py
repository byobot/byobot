"""What a relay device, such as a phone, may write into the byobot logbook. Every transport (the hub, later
Drive) checks what a device sends with these, so the rules can't drift apart."""

from __future__ import annotations

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
