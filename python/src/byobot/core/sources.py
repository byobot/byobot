"""The source lane: connectors write each record they fetch as a revision of a `source_record`, every revision kept."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..util import ValidationFailed, plain_text
from . import mappers
from .contract import documents, mac
from .store import Store, stamp

ACTOR_KIND = "connector"


def record_id(store: Store, source: str, source_type: str, source_id: str) -> str:
    return store.contract.derived("source_record", [source, source_type, str(source_id)])


def ingest(store: Store, source: str, records: Iterable[tuple[str, str, dict]],
           deleted: Iterable[tuple[str, str]] = ()) -> list[dict]:
    """Write what a source gave, all or none: (source_type, source_id, raw) revisions, and the (source_type,
    source_id) it says it deleted. A deletion of a record not held yet is kept, so the record never shows. A revision
    already held, the current one with only a newer revision time, a revision of an erased or deleted record, or a
    deletion already held, writes nothing. A changed health sample without a revision time is stamped with when it
    was imported, so it is current."""
    key = store.store_key
    changes, seen = [], set()
    for source_type, source_id, raw in records:
        payload = {"source": source, "source_type": source_type, "source_id": str(source_id), "raw": raw}
        rid = record_id(store, source, source_type, source_id)
        payload = _stamped(store, rid, payload)
        event_id = store.contract.derived("source_revision", [rid, mac(key, payload)])
        if event_id in seen or store.event(event_id) is not None or _erased(store, rid) or _restamped(store, rid, raw) \
                or (_tombstoned(store, rid) and not mappers.revised(payload)):
            continue
        seen.add(event_id)
        changes.append({"kind": "source_record", "id": rid, "op": "put", "payload": payload, "prev": [],
                        "event_id": event_id})
    for source_type, source_id in deleted:
        rid = record_id(store, source, source_type, source_id)
        found = store.record("source_record", rid)
        prev = [found["current"]] if found and found["live"] else []
        event_id = store.contract.derived("source_deletion", [rid, *prev])
        if event_id in seen or store.event(event_id) is not None or (found and not prev):
            continue
        seen.add(event_id)
        changes.append({"kind": "source_record", "id": rid, "op": "retract", "payload": {}, "prev": prev,
                        "event_id": event_id})
    if not changes:
        return []
    try:
        return store.write({"kind": ACTOR_KIND, "id": source}, changes)
    except ValidationFailed as exc:
        raise ValidationFailed(f"{source}: {exc}") from None


SAMPLE_FIELDS = frozenset(documents()[0]["sync"]["$defs"]["sample"]["properties"])


def sample_type(hk_type: str) -> str:
    if hk_type == "HKWorkoutTypeIdentifier":
        return "workout"
    return "category_sample" if hk_type.startswith("HKCategoryType") else "quantity_sample"


def ingest_samples(store: Store, samples: list[dict], deleted: Iterable[dict] = ()) -> dict[str, list[dict]]:
    """Samples as a phone reads them from a health store, each its own source's record, and the samples it says
    were deleted ({source, source_id}, with `type` when known). Only the fields the contract declares are kept, so
    one a newer phone adds changes no revision. Each source is written all or none."""
    by_source: dict[str, tuple[list, list]] = {}
    for s in samples:
        if not isinstance(s, dict) or not all(isinstance(s.get(k), str) for k in ("source", "source_id", "type")):
            raise ValidationFailed("each sample needs a source, a source_id and a type")
        source = s["source"]
        kept = {k: v for k, v in s.items() if k in SAMPLE_FIELDS}
        for k in ("device", "origin_app"):  # apps name themselves, so these reach agents
            if isinstance(kept.get(k), str):
                kept[k] = plain_text(kept[k])
                if not kept[k]:
                    del kept[k]
        by_source.setdefault(source, ([], []))[0].append((sample_type(s["type"]), s["source_id"], kept))
    for d in deleted:
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("source", "source_id")):
            raise ValidationFailed("each deletion needs a source and a source_id")
        source = d["source"]
        types = [sample_type(d["type"])] if isinstance(d.get("type"), str) else [r[0] for r in store.conn.execute(
            "SELECT source_type FROM source_records WHERE source = ? AND source_id = ?", (source, d["source_id"]))]
        by_source.setdefault(source, ([], []))[1].extend((t, d["source_id"]) for t in types)
    return {source: ingest(store, source, records, gone) for source, (records, gone) in by_source.items()}


def _erased(store: Store, rid: str) -> bool:
    found = store.record("source_record", rid)
    return bool(found and found["erased"])


def _tombstoned(store: Store, rid: str) -> bool:
    return store.event(store.contract.derived("source_deletion", [rid])) is not None


def _stamped(store: Store, rid: str, payload: dict) -> dict:
    """A changed revision without a time of its own, stamped after the current one, so it is current."""
    found = store.get("source_record", rid)
    if mappers.revised(payload) or found is None or _restamped(store, rid, payload["raw"]):
        return payload
    at = store.wall().astimezone(timezone.utc)
    current = mappers.revision_order(found["event"])
    if current[0] and at.timestamp() <= current[1]:
        at = datetime.fromtimestamp(current[1], timezone.utc) + timedelta(milliseconds=1)
    return mappers.stamped(payload, stamp(at))


def _restamped(store: Store, rid: str, raw: dict) -> bool:
    """The current revision sent again with only a newer revision time, as a day's running total is."""
    found = store.get("source_record", rid)
    return found is not None and mappers.unstamped(found["payload"]["raw"]) == mappers.unstamped(raw)


def history(store: Store, rid: str) -> list[dict]:
    """Every revision of a source record, oldest first by the source's own account."""
    puts = [e for e in store.history("source_record", rid) if e["op"] == "put" and "redacted_by" not in e]
    return sorted(puts, key=mappers.revision_order)
