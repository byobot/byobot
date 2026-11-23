"""The byobot v0 contract: JSON Schemas shared by every implementation.

The schemas live in ``schema/v0`` at the repository root. Built packages
carry a copy in ``byobot/_schema``; a source checkout reads the root copy.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_VERSION = 0
SCHEMA_NAMES = ("event", "training", "telemetry", "common", "sync")

EVENT_TYPES = (
    "activity.defined",
    "location.defined",
    "plan.defined",
    "session.planned",
    "session.performed",
    "effort.performed",
    "event.undone",
)

# Which part of the contract checks each event type's payload.
PAYLOAD_REFS = {
    "activity.defined": ("training", "activity"),
    "location.defined": ("training", "location"),
    "plan.defined": ("training", "plan"),
    "session.planned": ("training", "planned_session"),
    "session.performed": ("training", "performed_session"),
    "effort.performed": ("training", "performed_effort"),
    "event.undone": ("event", "undo"),
}


def schema_dir() -> Path:
    here = Path(__file__).resolve().parent
    # Installed package, then a git checkout (python/src/byobot -> repo root), then an unpacked sdist.
    for candidate in (here / "_schema" / "v0", here.parents[2] / "schema" / "v0", here.parents[1] / "schema" / "v0"):
        if (candidate / "event.schema.json").is_file():
            return candidate
    raise RuntimeError("byobot schema files not found; reinstall the package")


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, dict], Registry]:
    schemas: dict[str, dict] = {}
    registry: Registry = Registry()
    for name in SCHEMA_NAMES:
        doc = json.loads((schema_dir() / f"{name}.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(doc)
        schemas[name] = doc
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    return schemas, registry


def schema(name: str) -> dict:
    schemas, _ = _load()
    if name not in schemas:
        raise KeyError(name)
    return schemas[name]


@lru_cache(maxsize=None)
def _validator(name: str, definition: str | None = None) -> Draft202012Validator:
    schemas, registry = _load()
    doc = schemas[name]
    if definition is not None:
        doc = {"$ref": f"{doc['$id']}#/$defs/{definition}"}
    return Draft202012Validator(doc, registry=registry)


def _errors(validator: Draft202012Validator, document: Any) -> list[dict]:
    out = []
    for err in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        out.append({"path": "$" + path, "message": err.message})
    return out


def validate(name: str, document: Any) -> list[dict]:
    """Check a whole document (``event`` or ``telemetry``). Returns a list of errors, empty if valid."""
    errors = _errors(_validator(name), document)
    if errors:
        return errors
    if name == "event":
        return _semantic(document["type"], document["payload"], "$.payload")
    if name == "telemetry":
        return _time_order(document, "start", "end", "$")
    return []


def validate_value(definition: str, value: Any) -> list[dict]:
    """Check one value against a shared definition in common.schema.json, such as ``id`` or ``date``."""
    return _errors(_validator("common", definition), value)


def validate_sync(definition: str, document: Any) -> list[dict]:
    """Check a sync protocol message (``sync.schema.json``), such as ``plan_window`` or ``events_request``."""
    return _errors(_validator("sync", definition), document)


def validate_payload(event_type: str, payload: Any) -> list[dict]:
    """Check just the payload for an event type."""
    if event_type not in PAYLOAD_REFS:
        return [{"path": "$", "message": f"unknown event type {event_type!r}"}]
    return _errors(_validator(*PAYLOAD_REFS[event_type]), payload) or _semantic(event_type, payload, "$")


# -- rules JSON Schema cannot express (see schema/README.md) -----------------------

def parse_timestamp(text: str) -> datetime:
    """Parse a contract timestamp on any supported Python (3.10 rejects 'Z' and 9-digit fractions)."""
    match = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", text)
    if not match:
        raise ValueError(f"not a timestamp: {text!r}")
    base, frac, offset = match.groups()
    micros = (frac or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{base}.{micros}{'+00:00' if offset == 'Z' else offset}")


def instant_key(text: str) -> str:
    """A timestamp as UTC with nine fraction digits, so plain string order is time order across offsets."""
    match = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", text)
    if not match:
        raise ValueError(f"not a timestamp: {text!r}")
    base, frac, offset = match.groups()
    whole = datetime.fromisoformat(f"{base}{'+00:00' if offset == 'Z' else offset}").astimezone(timezone.utc)
    return f"{whole:%Y-%m-%dT%H:%M:%S}.{(frac or '').ljust(9, '0')}Z"


def event_order_key(event: dict) -> tuple[str, str, str]:
    """The order events apply in: event time (latest wins), then device id for exact ties, then event id.

    Arrival order is kept in the store only as an audit trail, so two devices that
    sync the same events in different orders end up with the same state.
    """
    return (instant_key(event["ts"]), event["device_id"], event["id"])


def _time_order(doc: dict, start: str, end: str, path: str) -> list[dict]:
    if start in doc and end in doc and parse_timestamp(doc[end]) < parse_timestamp(doc[start]):
        return [{"path": f"{path}.{end}", "message": f"{end} is before {start}", "rule": "time-order"}]
    return []


def _range_order(node: Any, path: str) -> list[dict]:
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if key == "metrics" and isinstance(value, dict):
                for metric, measure in value.items():
                    if isinstance(measure, dict) and "min" in measure and "max" in measure and measure["min"] > measure["max"]:
                        out.append({"path": f"{here}.{metric}", "message": "min is greater than max", "rule": "range-order"})
            else:
                out.extend(_range_order(value, here))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            out.extend(_range_order(item, f"{path}[{i}]"))
    return out


def _semantic(event_type: str, payload: dict, path: str) -> list[dict]:
    errors = _range_order(payload, path)
    if event_type == "plan.defined" and "ends_on" in payload and payload["ends_on"] < payload["starts_on"]:
        errors.append({"path": f"{path}.ends_on", "message": "ends_on is before starts_on", "rule": "date-order"})
    if event_type == "session.performed":
        errors.extend(_time_order(payload, "started_at", "ended_at", path))
    return errors
