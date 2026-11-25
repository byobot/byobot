"""The authoring commands over the core store. They fill `prev` from what they read, so callers never do."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..util import Conflict, NotFound, ValidationFailed
from .store import Store, stamp

AUTHORED = ("activity", "location", "setting", "plan", "planned_session", "session", "effort", "observation")
TIMED = ("session", "effort", "observation")


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386: objects merge key by key, null removes a key, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def _authored(kind: str) -> None:
    if kind not in AUTHORED:
        raise ValidationFailed(f"{kind} records aren't written by hand")


def _home_zone(store: Store) -> tzinfo | None:
    found = store.get("setting", "home_zone")
    try:
        return ZoneInfo(found["payload"]["value"]) if found else None
    except (ZoneInfoNotFoundError, KeyError, TypeError, ValueError):
        return None


def _local(ts: Any, zone: tzinfo | None) -> Any:
    """A time without an offset is local: in the home zone if one is set, else this machine's; the offset in
    force then is written with it."""
    if not isinstance(ts, str):
        return ts
    try:
        at = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if at.tzinfo:
        return ts
    return stamp(at.replace(tzinfo=zone) if zone else at.astimezone())


def _timed(store: Store, kind: str, payload: Any) -> Any:
    """Times keep the offset they were written in: now, if none was given, and the local one if it was left off."""
    if kind not in TIMED or not isinstance(payload, dict):
        return payload
    t = payload.get("time")
    if t is None:
        return {**payload, "time": {"start": stamp(store.wall())}}
    if isinstance(t, dict):
        zone = _home_zone(store)
        return {**payload, "time": {k: _local(v, zone) if k in ("start", "end") else v for k, v in t.items()}}
    return payload


def log(store: Store, actor: dict, records: list[dict]) -> list[dict]:
    """Create records, all or none: [{kind, id?, payload}]. An id already in use is refused, never overwritten."""
    changes = []
    for r in records:
        _authored(r["kind"])
        rid = r.get("id") or store.new_record_id(r["kind"])
        found = store.record(r["kind"], rid)
        if found and (found["live"] or found["erased"]):
            raise Conflict(f"{r['kind']} {rid} {'was erased' if found['erased'] else 'already exists'}")
        payload = _timed(store, r["kind"], r["payload"])
        changes.append({"kind": r["kind"], "id": rid, "op": "put", "payload": payload})
    if len({(c["kind"], c["id"]) for c in changes}) != len(changes):
        raise ValidationFailed("one record twice in one log")
    return store.write(actor, changes)


def _live(store: Store, kind: str, rid: str) -> dict:
    found = store.get(kind, rid)
    if found is None:
        raise NotFound(f"no {kind} {rid}")
    return found


def edit(store: Store, actor: dict, kind: str, rid: str, patch: dict) -> dict:
    """Change a live record by a JSON merge patch over its current version."""
    _authored(kind)
    payload = _timed(store, kind, merge_patch(_live(store, kind, rid)["payload"], patch))
    return store.write(actor, [{"kind": kind, "id": rid, "op": "put", "payload": payload}])[0]


def replan(store: Store, actor: dict, rid: str, payload: dict) -> dict:
    """Write a planned session's new version. Blocks and planned efforts given without ids keep the ids at
    their positions, so efforts already logged against them stay linked."""
    current = store.get("planned_session", rid)
    old = current["payload"]["blocks"] if current else []
    blocks = []
    for bi, block in enumerate(payload.get("blocks", [])):
        was = old[bi] if bi < len(old) else {"efforts": []}
        efforts = [{"id": was["efforts"][ei]["id"] if ei < len(was["efforts"]) else f"{rid}.{bi}.{ei}", **e}
                   for ei, e in enumerate(block.get("efforts", []))]
        blocks.append({"id": was.get("id", f"{rid}.{bi}"), **block, "efforts": efforts})
    return store.write(actor, [{"kind": "planned_session", "id": rid, "op": "put",
                                "payload": {**payload, "blocks": blocks}}])[0]


def remove(store: Store, actor: dict, kind: str, rid: str, reason: str | None = None) -> dict:
    _authored(kind)
    _live(store, kind, rid)
    payload = {"reason": reason} if reason else {}
    return store.write(actor, [{"kind": kind, "id": rid, "op": "retract", "payload": payload}])[0]


def undo(store: Store, actor: dict, event_id: str) -> list[dict]:
    """Take an event back by writing its record's previous version (a retract if it had none).

    An event written in a batch is undone with the rest of its batch.
    """
    target = store.event(event_id)
    if target is None:
        raise NotFound(f"no event {event_id}")
    batch = store.batch(target["batch"]) if target.get("batch") else [target]
    changes = []
    for e in batch:
        kind, rid = e["record"]["kind"], e["record"]["id"]
        if e["op"] not in ("put", "retract"):
            raise ValidationFailed(f"can't undo a {e['op']}")
        _authored(kind)
        before = [store.event(p) for p in e["prev"]]
        if any(b is None for b in before):
            raise Conflict(f"can't undo {e['id']}: its previous version isn't here yet")
        base = max(before, key=lambda b: (b["hlc"], b["id"]), default=None)
        if base is None or base["op"] == "retract":
            changes.append({"kind": kind, "id": rid, "op": "retract", "payload": {}, "reverts": e["id"]})
        else:
            changes.append({"kind": kind, "id": rid, "op": "put", "payload": base["payload"],
                            "kind_version": base["kind_version"], "reverts": e["id"]})
    return store.write(actor, changes)


def conflicts(store: Store, kind: str | None = None, rid: str | None = None) -> list[dict]:
    return store.conflicts(kind, rid)


def resolve(store: Store, actor: dict, kind: str, rid: str, choice: str | dict) -> dict:
    """End a conflict: keep one version (its event id), "remove" the record, or write a combined payload."""
    found = store.conflicts(kind, rid)
    if not found:
        raise NotFound(f"{kind} {rid} has no conflict")
    versions = {v["id"]: v for v in found[0]["versions"]}
    if isinstance(choice, dict):
        change = {"op": "put", "payload": _timed(store, kind, choice)}
    elif choice == "remove":
        change = {"op": "retract", "payload": {}}
    elif choice in versions:
        picked = versions[choice]
        change = {"op": picked["op"], "payload": picked["payload"] if picked["op"] == "put" else {}}
        if picked["op"] == "put":
            change["kind_version"] = picked["kind_version"]
    else:
        raise ValidationFailed(f"{choice} is not a version of {kind} {rid}")
    return store.write(actor, [{"kind": kind, "id": rid, **change}])[0]
