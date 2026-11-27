"""The authoring commands over the core store. Each write names the version it was made from as `prev`, so a
version written meanwhile stays a visible conflict."""

from __future__ import annotations

import json
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import library
from ..util import Conflict, NotFound, ValidationFailed
from . import mappers
from .contract import instant
from .store import Store, stamp

AUTHORED = ("activity", "location", "setting", "plan", "planned_session", "session", "effort", "observation")
TIMED = ("session", "effort", "observation")
TAKEN_BACK = (*AUTHORED, "overlay")  # what remove and undo work on
ERASABLE = (*(k for k in AUTHORED if k != "setting"), "source_record")  # an erased id can't be written again


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


def check_activity_id(rid: Any) -> None:
    """Your own activities are `x.` ids; a plain id is the library's, which yours may replace."""
    if isinstance(rid, str) and not rid.startswith("x.") and rid not in {a["id"] for a in library.activities()}:
        raise ValidationFailed(f"{rid} isn't a library activity; your own activities' ids start with x., as in x.{rid}")


def _home_zone(store: Store) -> tzinfo | None:
    found = store.get("setting", "home_zone")
    try:
        return ZoneInfo(found["payload"]["value"]) if found else None
    except (ZoneInfoNotFoundError, KeyError, TypeError, ValueError):
        return None


def _local(ts: Any, zone: tzinfo | None) -> Any:
    """A time without an offset is local: in the home zone if one is set, else this machine's; the offset in
    force then is written with it. A time in UTC ("Z") is the same instant in the home zone, if one is set."""
    if not isinstance(ts, str):
        return ts
    utc = ts.endswith(("Z", "z"))
    try:
        at = datetime.fromisoformat(ts[:-1] + "+00:00" if utc else ts)
    except ValueError:
        return ts
    if at.tzinfo and zone and utc:
        return stamp(at.astimezone(zone))
    if at.tzinfo:
        return ts
    try:
        return stamp(at.replace(tzinfo=zone) if zone else at.astimezone())
    except (OverflowError, ValueError, OSError):  # Windows raises OSError past its clock's range
        raise ValidationFailed(f"{ts!r} is out of range") from None


def _timed(store: Store, kind: str, payload: Any) -> Any:
    """Times keep the offset they were written in: now, if none was given, and the local one if it was left off."""
    if kind not in TIMED or not isinstance(payload, dict):
        return payload
    if "modality" not in payload:
        payload = {**payload, "modality": "self_reported"}
    if payload.get("time") is None:
        return {**payload, "time": {"start": stamp(store.wall())}}
    return _times(store, payload)


def _times(store: Store, fields: dict) -> dict:
    """`fields` with a start and end left without an offset made local."""
    t = fields.get("time")
    if not isinstance(t, dict):
        return fields
    zone = _home_zone(store)
    return {**fields, "time": {k: _local(v, zone) if k in ("start", "end") else v for k, v in t.items()}}


def _written(store: Store, kind: str, payload: Any, before: dict | None = None) -> Any:
    """The payload as a command writes it: times filled in, and held to the registry, except where it keeps
    what `before` already held."""
    payload = _timed(store, kind, payload)
    refused = store.contract.refusals(kind, payload) if isinstance(payload, dict) else []
    if refused and before:
        held = set(store.contract.refusals(kind, before))
        refused = [r for r in refused if r not in held]
    if refused:
        raise ValidationFailed("; ".join(refused))
    return payload


def log(store: Store, actor: dict, records: list[dict]) -> list[dict]:
    """Create records, all or none: [{kind, id?, payload}]. An id already in use is refused, never overwritten."""
    changes = []
    for r in records:
        _authored(r["kind"])
        rid = r.get("id") or store.new_record_id(r["kind"])
        if r["kind"] == "activity":
            check_activity_id(rid)
        payload = r["payload"]
        if r["kind"] == "planned_session" and isinstance(payload, dict):
            payload = _parts(rid, payload, [])
        payload = _written(store, r["kind"], payload)
        changes.append({"kind": r["kind"], "id": rid, "op": "put", "payload": payload, "create": True})
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
    fields = {k: v for k, v in merge_patch(found["payload"]["fields"] if found else {}, fields).items() if v != {}}
    if not fields:
        raise ValidationFailed("nothing left to correct; remove the correction instead")
    fields = _times(store, fields)
    _time_order(store, target, fields.get("time"))
    return _overlay(store, actor, "correct", target, {"fields": fields}, found)


def _time_order(store: Store, target: str, time: Any) -> None:
    """Refuse a corrected start or end that, with the source's other end, runs backwards."""
    source = store.get("source_record", target)
    if not isinstance(time, dict) or source is None:
        return
    try:
        session = mappers.read(source["payload"], lambda *_: None)["session"]
        merged = {**(session or {}).get("time", {}), **time}
        backwards = "end" in merged and instant(merged["end"]) < instant(merged["start"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return
    if backwards:
        raise ValidationFailed("the corrected time would end before it starts; correct both start and end")


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
    payload = _written(store, kind, merge_patch(found["payload"], patch), found["payload"])
    return store.write(actor, [{"kind": kind, "id": rid, "op": "put", "payload": payload, **_from(found)}])[0]


def replan(store: Store, actor: dict, rid: str, payload: dict) -> dict:
    """Write a planned session's new version. Blocks and planned efforts given without ids keep the ids at
    their positions, so efforts already logged against them stay linked; a planned effort only if it's the same
    activity."""
    current = store.get("planned_session", rid)
    payload = _parts(rid, payload, current["payload"]["blocks"] if current else [])
    return store.write(actor, [{"kind": "planned_session", "id": rid, "op": "put",
                                "payload": _written(store, "planned_session", payload,
                                                    current and current["payload"]), **_from(current)}])[0]


def _parts(rid: str, payload: dict, old: list) -> dict:
    """Ids for blocks and planned efforts given without them: the ones at their positions in `old` (a planned
    effort's only if it's the same activity, so logged sets never move to another exercise), else new."""
    if not isinstance(payload.get("blocks"), list):
        return payload
    taken = {e.get("id") for b in old for e in b.get("efforts", [])}
    blocks = []
    for bi, block in enumerate(payload["blocks"]):
        if not isinstance(block, dict) or not isinstance(block.get("efforts", []), list):
            return payload
        was = old[bi] if bi < len(old) else {"efforts": []}
        efforts = []
        for ei, e in enumerate(block.get("efforts", [])):
            if isinstance(e, dict) and "id" not in e:
                prior = was["efforts"][ei] if ei < len(was["efforts"]) else None
                if prior and prior.get("activity_id") == e.get("activity_id"):
                    e = {"id": prior["id"], **e}
                else:
                    e = {"id": _unused(f"{rid}.{bi}.{ei}", taken), **e}
                taken.add(e["id"])
            efforts.append(e)
        blocks.append({"id": was.get("id", f"{rid}.{bi}"), **block, "efforts": efforts})
    return {**payload, "blocks": blocks}


def _unused(base: str, taken: set) -> str:
    n, rid = 1, base
    while rid in taken:
        n += 1
        rid = f"{base}.{n}"
    return rid


def remove(store: Store, actor: dict, kind: str, rid: str, reason: str | None = None) -> dict:
    if kind not in TAKEN_BACK:
        raise ValidationFailed(f"{kind} records aren't removed by hand")
    found = _live(store, kind, rid)
    payload = {"reason": reason} if reason else {}
    return store.write(actor, [{"kind": kind, "id": rid, "op": "retract", "payload": payload, **_from(found)}])[0]


def erase(store: Store, actor: dict, kind: str, rid: str, with_efforts: bool = False) -> list[dict]:
    """Destroy a record's content everywhere, with every overlay on it, and a session's efforts if asked.
    Can't be undone."""
    if kind == "setting":
        raise ValidationFailed("a setting can't be erased, or it could never be set again; remove it instead")
    if kind not in ERASABLE:
        raise ValidationFailed(f"{kind} records can't be erased on their own")
    found = store.record(kind, rid)
    if found is None:
        raise NotFound(f"no {kind} {rid}")
    if found["erased"]:
        raise Conflict(f"{kind} {rid} was already erased")
    if with_efforts and kind != "session":
        raise ValidationFailed("only a session has efforts to erase with it")
    targets = [(kind, rid)]
    if with_efforts:
        targets += [e for e in linked(store, kind, rid, "part_of") if e[0] == "effort"]
    changes = []
    for k, r in targets:
        changes.append({"kind": k, "id": r, "op": "erase"})
        overlays = [o[0] for o in store.conn.execute(
            "SELECT DISTINCT record_id FROM events WHERE kind = 'overlay' AND json_extract(payload, '$.target.kind') = ? "
            "AND json_extract(payload, '$.target.id') = ? ORDER BY record_id", (k, r))]
        changes += [{"kind": "overlay", "id": o, "op": "erase"} for o in overlays
                    if not store.record("overlay", o)["erased"]]
    return store.write(actor, changes)


def linked(store: Store, kind: str, rid: str, link: str | None = None) -> list[tuple[str, str]]:
    """Records not erased that any version links to this one, by any link or the one named."""
    out = set()
    for k, r, payload in store.conn.execute(
            "SELECT e.kind, e.record_id, e.payload FROM events e JOIN records r ON r.kind = e.kind "
            "AND r.id = e.record_id WHERE r.erased = 0 AND e.op = 'put' AND instr(e.payload, ?) > 0", (rid,)):
        links = json.loads(payload).get("links") or {}
        for name, target in links.items():
            if (link is None or name == link) and isinstance(target, dict) and \
                    (target.get("kind"), target.get("id")) == (kind, rid):
                out.add((k, r))
    return sorted(out)


def undo(store: Store, actor: dict, event_id: str) -> list[dict]:
    """Take an event back by writing its record's previous version (a retract if it had none). Only the
    record's one current version can be undone, so a later edit is never thrown away.

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
        if kind not in TAKEN_BACK:
            raise ValidationFailed(f"{kind} records aren't undone by hand")
        found = store.record(kind, rid)
        if found["heads"] != [e["id"]]:
            raise Conflict(f"{kind} {rid} changed after {e['id']}; edit it instead")
        before = [store.event(p) for p in e["prev"]]
        if any(b is None for b in before):
            raise Conflict(f"can't undo {e['id']}: its previous version isn't here yet")
        base = max(before, key=lambda b: (b["hlc"], b["id"]), default=None)
        if base is None or base["op"] == "retract":
            changes.append({"kind": kind, "id": rid, "op": "retract", "payload": {}, "reverts": e["id"],
                            "prev": [e["id"]]})
        else:
            changes.append({"kind": kind, "id": rid, "op": "put", "payload": base["payload"],
                            "kind_version": base["kind_version"], "reverts": e["id"], "prev": [e["id"]]})
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
