"""The source lane: connectors write each record they fetch as a revision of a `source_record`, every revision kept."""

from __future__ import annotations

from typing import Iterable

from ..util import ValidationFailed
from . import mappers
from .contract import mac
from .store import Store

ACTOR_KIND = "connector"


def record_id(store: Store, source: str, source_type: str, source_id: str) -> str:
    return store.contract.derived("source_record", [source, source_type, str(source_id)])


def ingest(store: Store, source: str, records: Iterable[tuple[str, str, dict]],
           deleted: Iterable[tuple[str, str]] = ()) -> list[dict]:
    """Write what a source gave, all or none: (source_type, source_id, raw) revisions, and the (source_type,
    source_id) it says it deleted. A revision already held, the current one with only a newer revision time, or a
    deletion of a record not held, writes nothing."""
    key = store.store_key
    changes, seen = [], set()
    for source_type, source_id, raw in records:
        payload = {"source": source, "source_type": source_type, "source_id": str(source_id), "raw": raw}
        rid = record_id(store, source, source_type, source_id)
        event_id = store.contract.derived("source_revision", [rid, mac(key, payload)])
        if event_id in seen or store.event(event_id) is not None or _restamped(store, rid, raw):
            continue
        seen.add(event_id)
        changes.append({"kind": "source_record", "id": rid, "op": "put", "payload": payload, "prev": [],
                        "event_id": event_id})
    for source_type, source_id in deleted:
        rid = record_id(store, source, source_type, source_id)
        found = store.record("source_record", rid)
        if not found or not found["live"]:
            continue
        prev = [found["current"]]
        changes.append({"kind": "source_record", "id": rid, "op": "retract", "payload": {}, "prev": prev,
                        "event_id": store.contract.derived("source_deletion", [rid, prev[0]])})
    if not changes:
        return []
    try:
        return store.write({"kind": ACTOR_KIND, "id": source}, changes)
    except ValidationFailed as exc:
        raise ValidationFailed(f"{source}: {exc}") from None


SAMPLE_SOURCES = {"apple-health": "apple_health"}  # v0's spelling, still sent by older phones


def sample_type(hk_type: str) -> str:
    if hk_type == "HKWorkoutTypeIdentifier":
        return "workout"
    return "category_sample" if hk_type.startswith("HKCategoryType") else "quantity_sample"


def ingest_samples(store: Store, samples: list[dict], deleted: Iterable[dict] = ()) -> dict[str, list[dict]]:
    """Samples as a phone reads them from a health store, each its own source's record, and the samples it says
    were deleted ({source, source_id}, with `type` when known). Each source is written all or none."""
    by_source: dict[str, tuple[list, list]] = {}
    for s in samples:
        if not isinstance(s, dict) or not all(isinstance(s.get(k), str) for k in ("source", "source_id", "type")):
            raise ValidationFailed("each sample needs a source, a source_id and a type")
        source = SAMPLE_SOURCES.get(s["source"], s["source"])
        by_source.setdefault(source, ([], []))[0].append((sample_type(s["type"]), s["source_id"], s))
    for d in deleted:
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("source", "source_id")):
            raise ValidationFailed("each deletion needs a source and a source_id")
        source = SAMPLE_SOURCES.get(d["source"], d["source"])
        types = [sample_type(d["type"])] if isinstance(d.get("type"), str) else [r[0] for r in store.conn.execute(
            "SELECT source_type FROM source_records WHERE source = ? AND source_id = ?", (source, d["source_id"]))]
        by_source.setdefault(source, ([], []))[1].extend((t, d["source_id"]) for t in types)
    return {source: ingest(store, source, records, gone) for source, (records, gone) in by_source.items()}


def _restamped(store: Store, rid: str, raw: dict) -> bool:
    """The current revision sent again with only a newer revision time, as a day's running total is."""
    found = store.get("source_record", rid)
    return found is not None and mappers.unstamped(found["payload"]["raw"]) == mappers.unstamped(raw)


def history(store: Store, rid: str) -> list[dict]:
    """Every revision of a source record, oldest first by the source's own account."""
    puts = [e for e in store.history("source_record", rid) if e["op"] == "put" and "redacted_by" not in e]
    return sorted(puts, key=mappers.revision_order)
