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
    source_id) it says it deleted. A revision already held, or a deletion of a record not held, writes nothing."""
    key = store.store_key
    changes, seen = [], set()
    for source_type, source_id, raw in records:
        payload = {"source": source, "source_type": source_type, "source_id": str(source_id), "raw": raw}
        rid = record_id(store, source, source_type, source_id)
        event_id = store.contract.derived("source_revision", [rid, mac(key, payload)])
        if event_id in seen or store.event(event_id) is not None:
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


def history(store: Store, rid: str) -> list[dict]:
    """Every revision of a source record, oldest first by the source's own account."""
    puts = [e for e in store.history("source_record", rid) if e["op"] == "put"]
    return sorted(puts, key=lambda e: (mappers.revised(e["payload"]), e["id"]))
