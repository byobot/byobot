"""The authoring commands over the core store. Each write names the version it was made from as `prev`, so a
version written meanwhile stays a visible conflict."""

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
    if "modality" not in payload:
        payload = {**payload, "modality": "self_reported"}
    t = payload.get("time")
    if t is None:
        return {**payload, "time": {"start": stamp(store.wall())}}
    if isinstance(t, dict):
        zone = _home_zone(store)
        return {**payload, "time": {k: _local(v, zone) if k in ("start", "end") else v for k, v in t.items()}}
    return payload


def _written(store: Store, kind: str, payload: Any) -> Any:
    """The payload as a command writes it: times filled in, and held to the registry."""
    payload = _timed(store, kind, payload)
    refused = store.contract.refusals(kind, payload) if isinstance(payload, dict) else []
    if refused:
        raise ValidationFailed("; ".join(refused))
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
        payload = r["payload"]
        if r["kind"] == "planned_session" and isinstance(payload, dict):
            payload = _parts(rid, payload, [])
        payload = _written(store, r["kind"], payload)
        changes.append({"kind": r["kind"], "id": rid, "op": "put", "payload": payload})
    if len({(c["kind"], c["id"]) for c in changes}) != len(changes):
        raise ValidationFailed("one record twice in one log")
    return store.write(actor, changes)


def observe(store: Store, actor: dict, code: str, value: Any, unit: str | None = None, start: str | None = None,
            end: str | None = None, modality: str = "self_reported", notes: str | None = None) -> dict:
    """Log one observation, such as a weight: `start` defaults to now, and a time without an offset is local."""
    time = {k: v for k, v in (("start", start), ("end", end)) if v is not None} or None
    payload = {"code": code, "value": value, "unit": unit, "time": time, "modality": modality, "notes": notes}
    payload = {k: v for k, v in payload.items() if v is not None}
    return log(store, actor, [{"kind": "observation", "payload": payload}])[0]


def _overlay(store: Store, actor: dict, effect: str, target: str, body: dict, read: dict | None = None) -> dict:
    rid = store.contract.derived("overlay", [effect, "source_record", target])
    payload = {"effect": effect, "target": {"kind": "source_record", "id": target}, **body}
    refused = store.contract.refusals("overlay", payload)
    if refused:
        raise ValidationFailed("; ".join(refused))
    change = {"kind": "overlay", "id": rid, "op": "put", "payload": payload, **_from(read)}
    return store.write(actor, [change])[0]


def _from(read: dict | None) -> dict:
    """The `prev` of a write made from what `get` returned."""
    return {"prev": [read["event"]["id"]]} if read else {}


def correct(store: Store, actor: dict, target: str, fields: dict) -> dict:
    """Use your values over a source record's, whatever it later revises. `fields` merge into earlier corrections;
    a null takes one back."""
    found = store.get("overlay", store.contract.derived("overlay", ["correct", "source_record", target]))
    fields = merge_patch(found["payload"]["fields"] if found else {}, fields)
    if not fields:
        raise ValidationFailed("nothing left to correct; remove the correction instead")
    if "time" in fields:
        fields = _timed(store, "session", fields)
    return _overlay(store, actor, "correct", target, {"fields": fields}, found)


def exclude(store: Store, actor: dict, target: str, notes: str | None = None) -> dict:
    """Leave a source record out of every read."""
    return _overlay(store, actor, "exclude", target, {"notes": notes} if notes else {})


def same_as(store: Store, actor: dict, target: str, kind: str, rid: str) -> dict:
    """Say a source record is the same thing as another record, so reads count it once."""
    return _overlay(store, actor, "same_as", target, {"with": {"kind": kind, "id": rid}})


def _live(store: Store, kind: str, rid: str) -> dict:
    found = store.get(kind, rid)
    if found is None:
        raise NotFound(f"no {kind} {rid}")
    return found


def edit(store: Store, actor: dict, kind: str, rid: str, patch: dict) -> dict:
    """Change a live record by a JSON merge patch over its current version."""
    _authored(kind)
    found = _live(store, kind, rid)
    payload = _written(store, kind, merge_patch(found["payload"], patch))
    return store.write(actor, [{"kind": kind, "id": rid, "op": "put", "payload": payload, **_from(found)}])[0]


def replan(store: Store, actor: dict, rid: str, payload: dict) -> dict:
    """Write a planned session's new version. Blocks and planned efforts given without ids keep the ids at
    their positions, so efforts already logged against them stay linked."""
    current = store.get("planned_session", rid)
    payload = _parts(rid, payload, current["payload"]["blocks"] if current else [])
    return store.write(actor, [{"kind": "planned_session", "id": rid, "op": "put",
                                "payload": _written(store, "planned_session", payload), **_from(current)}])[0]


def _parts(rid: str, payload: dict, old: list) -> dict:
    """Ids for blocks and planned efforts given without them: the ones at their positions in `old`, else new."""
    if not isinstance(payload.get("blocks"), list):
        return payload
    blocks = []
    for bi, block in enumerate(payload["blocks"]):
        if not isinstance(block, dict) or not isinstance(block.get("efforts", []), list):
            return payload
        was = old[bi] if bi < len(old) else {"efforts": []}
        efforts = [{"id": was["efforts"][ei]["id"] if ei < len(was["efforts"]) else f"{rid}.{bi}.{ei}", **e}
                   if isinstance(e, dict) else e for ei, e in enumerate(block.get("efforts", []))]
        blocks.append({"id": was.get("id", f"{rid}.{bi}"), **block, "efforts": efforts})
    return {**payload, "blocks": blocks}


def remove(store: Store, actor: dict, kind: str, rid: str, reason: str | None = None) -> dict:
    _authored(kind)
    found = _live(store, kind, rid)
    payload = {"reason": reason} if reason else {}
    return store.write(actor, [{"kind": kind, "id": rid, "op": "retract", "payload": payload, **_from(found)}])[0]


def erase(store: Store, actor: dict, kind: str, rid: str) -> list[dict]:
    """Destroy a record's content everywhere, with every overlay on it. Can't be undone."""
    if kind not in AUTHORED and kind != "source_record":
        raise ValidationFailed(f"{kind} records can't be erased on their own")
    found = store.record(kind, rid)
    if found is None:
        raise NotFound(f"no {kind} {rid}")
    if found["erased"]:
        raise Conflict(f"{kind} {rid} was already erased")
    overlays = [r[0] for r in store.conn.execute(
        "SELECT DISTINCT record_id FROM events WHERE kind = 'overlay' AND json_extract(payload, '$.target.kind') = ? "
        "AND json_extract(payload, '$.target.id') = ? ORDER BY record_id", (kind, rid))]
    changes = [{"kind": kind, "id": rid, "op": "erase"}]
    changes += [{"kind": "overlay", "id": o, "op": "erase"} for o in overlays if not store.record("overlay", o)["erased"]]
    return store.write(actor, changes)


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


def resolve(store: Store, actor: dict, kind: str, rid: str, choice: str | dict, seen: list[str] | None = None) -> dict:
    """End a conflict: keep one version (its event id), "remove" the record, or write a combined payload.

    `seen` is the versions the person was shown; a version that arrived since keeps the conflict open.
    """
    found = store.conflicts(kind, rid)
    if not found:
        raise NotFound(f"{kind} {rid} has no conflict")
    versions = {v["id"]: v for v in found[0]["versions"]}
    if seen is not None:
        held = {e["id"] for e in store.history(kind, rid)}
        if not isinstance(seen, list) or not seen or not all(isinstance(i, str) and i in held for i in seen):
            raise ValidationFailed(f"seen must list versions of {kind} {rid}")
    prev = sorted(set(seen)) if seen is not None else list(versions)
    if isinstance(choice, dict):
        change = {"op": "put", "payload": _written(store, kind, choice)}
    elif choice == "remove":
        change = {"op": "retract", "payload": {}}
    elif choice in versions:
        picked = versions[choice]
        change = {"op": picked["op"], "payload": picked["payload"] if picked["op"] == "put" else {}}
        if picked["op"] == "put":
            change["kind_version"] = picked["kind_version"]
    else:
        raise ValidationFailed(f"{choice} is not a version of {kind} {rid}")
    if isinstance(choice, str) and choice in versions and choice not in prev:
        raise ValidationFailed(f"{choice} is not one of the versions you saw")
    return store.write(actor, [{"kind": kind, "id": rid, "prev": prev, **change}])[0]
