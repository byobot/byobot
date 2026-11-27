"""The schema/v1 contract: validating events, the rules beyond the schemas, derived ids and outcomes."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry, Resource

from ..util import ValidationFailed

SCHEMA_VERSION = 1
SCHEMA_NAMES = ("common", "records", "event", "registry", "sync", "cloud")
SLUG_KINDS = ("activity", "location", "setting")
RULES = ("time_order", "date_order", "range_order", "code_value", "part_ids", "self_reference", "derived_id")


def schema_dir() -> Path:
    here = Path(__file__).resolve().parent.parent
    for candidate in (here / "_schema" / "v1", here.parents[2] / "schema" / "v1", here.parents[1] / "schema" / "v1"):
        if (candidate / "event.schema.json").is_file():
            return candidate
    raise RuntimeError("byobot schema/v1 files not found; reinstall the package")


@lru_cache(maxsize=1)
def documents() -> tuple[dict[str, dict], dict]:
    """The shipped schemas by name, and the shipped registry."""
    docs = {n: json.loads((schema_dir() / f"{n}.schema.json").read_text(encoding="utf-8")) for n in SCHEMA_NAMES}
    return docs, json.loads((schema_dir() / "registry.json").read_text(encoding="utf-8"))


class NotFinite(ValidationFailed, ValueError):
    code = "invalid"


def jcs(value: Any) -> str:
    """RFC 8785 canonical JSON for the values byobot writes: sorted keys, no whitespace, 225.0 written as 225."""
    try:
        return json.dumps(_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise NotFinite("numbers must be finite, not Infinity or NaN") from None


def _numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return int(value)
    if isinstance(value, dict):
        return {k: _numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_numbers(v) for v in value]
    return value


def instant(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def sync_errors(definition: str, document: Any) -> list[str]:
    """Why a sync protocol message (a `$defs` entry of sync.schema.json, such as plan_window) is malformed."""
    return _errors("sync", definition, document)


def cloud_errors(definition: str, document: Any) -> list[str]:
    """Why a cloud protocol file or message (a `$defs` entry of cloud.schema.json) is malformed."""
    return _errors("cloud", definition, document)


def _errors(schema: str, definition: str, document: Any) -> list[str]:
    return [f"{'/'.join(str(p) for p in e.absolute_path) or 'message'}: {e.message}"
            for e in _validator(schema, definition).iter_errors(document)]


@lru_cache(maxsize=None)
def _validator(schema: str, definition: str) -> Draft202012Validator:
    docs = documents()[0]
    resources = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs.values())
    return Draft202012Validator({"$ref": f"{docs[schema]['$id']}#/$defs/{definition}"}, registry=resources)


@dataclass
class Contract:
    """The schemas and registry one install knows. `registry` may be newer than the shipped one."""

    registry: dict
    schemas: dict[str, dict] = field(default_factory=lambda: documents()[0])

    @classmethod
    def shipped(cls) -> Contract:
        return _shipped()

    @cached_property
    def _validator(self) -> Draft202012Validator:
        resources = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in self.schemas.values())
        return Draft202012Validator(self.schemas["event"], registry=resources)

    @cached_property
    def _later_validator(self) -> Draft202012Validator:
        resources = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in self.schemas.values())
        return Draft202012Validator({"$ref": f"{self.schemas['event']['$id']}#/$defs/later"}, registry=resources)

    def version(self, kind: str) -> int | None:
        known = self.registry["kinds"].get(kind)
        return known and known["version"]

    def namespace(self, name: str) -> uuid.UUID:
        return uuid.UUID(self.registry["namespaces"][name])

    def derived(self, namespace: str, parts: list) -> str:
        return str(uuid.uuid5(self.namespace(namespace), jcs(parts)))

    def check(self, event: dict) -> None:
        """Raise Rejected if the event breaks the schemas or a rule."""
        if later(event):
            error = best_match(self._later_validator.iter_errors(event))
            if error is not None:
                raise Rejected(f"{'/'.join(str(p) for p in error.absolute_path) or 'event'}: {error.message}")
            return
        error = best_match(self._validator.iter_errors(event))
        if error is not None:
            where = "/".join(str(p) for p in error.absolute_path) or "event"
            raise Rejected(f"{where}: {error.message}")
        try:
            broken = self.broken_rules(event)
        except (KeyError, TypeError, ValueError, AttributeError):
            if self.understands(event):
                raise Rejected("payload can't be read") from None
            broken = set()  # a newer payload: its own install checked it, and this one checks it on upgrade
        if broken:
            raise Rejected("broken rule: " + ", ".join(sorted(broken)))

    def payload_errors(self, kind: str, payload: Any, rid: str | None = None) -> list[str]:
        """Why a payload can't be written as a `kind` record, checked as a put by a placeholder author."""
        slug = kind in SLUG_KINDS
        event = {"id": PLACEHOLDER_ID, "schema_version": 1, "op": "put", "kind_version": self.version(kind) or 1,
                 "record": {"kind": kind, "id": rid or ("placeholder" if slug else PLACEHOLDER_ID)},
                 "hlc": "0000000000000-000000", "ts": "2026-01-01T00:00:00Z", "device_id": "check",
                 "actor": {"kind": "human", "id": "check"}, "prev": [], "payload": payload}
        try:
            self.check(event)
        except Rejected as exc:
            return [exc.reason.removeprefix("payload: ").removeprefix("payload/")]
        return self.refusals(kind, payload) if isinstance(payload, dict) else []

    def understands(self, event: dict) -> bool:
        """Whether this install can project the event (it is stored either way)."""
        if later(event):
            return False
        kind, op = event["record"]["kind"], event["op"]
        known = self.version(kind)
        if known is None or (op == "put" and event["kind_version"] > known):
            return False
        return not (kind == "setting" and op == "put" and event["record"]["id"] not in self.registry["settings"])

    def broken_rules(self, event: dict) -> set[str]:
        broken = set()
        if event["id"] in event["prev"] or event.get("reverts") == event["id"]:
            broken.add("self_reference")
        if event["op"] != "put" or "redacted_by" in event:
            return broken
        kind, rid, p = event["record"]["kind"], event["record"]["id"], event["payload"]
        for t in (p.get("time"), (p.get("fields") or {}).get("time")):
            times = [instant(t[k]) for k in ("start", "end") if k in t] if isinstance(t, dict) else []
            if len(times) == 2 and times[1] < times[0]:
                broken.add("time_order")
        for k in ("starts_on", "ends_on", "scheduled_on"):
            if k in p:
                date.fromisoformat(p[k])  # a day that doesn't exist can't be read
        if kind == "plan" and "ends_on" in p and p["ends_on"] < p["starts_on"]:
            broken.add("date_order")
        for metrics in _measures(kind, p):
            for code, m in metrics.items():
                bounds = [m.get("min"), m.get("value") if "min" in m or "max" in m else None, m.get("max")]
                bounds = [x for x in bounds if _number(x)]
                if bounds != sorted(bounds):
                    broken.add("range_order")
                if not self.code_ok(code, m):
                    broken.add("code_value")
        if kind == "planned_session":
            parts = [b["id"] for b in p["blocks"]] + [e["id"] for b in p["blocks"] for e in b["efforts"]]
            if len(parts) != len(set(parts)):
                broken.add("part_ids")
        if kind == "source_record" and {"source", "source_type", "source_id"} <= set(p) and \
                rid != self.derived("source_record", [p["source"], p["source_type"], p["source_id"]]):
            broken.add("derived_id")
        if kind == "overlay" and "effect" in p and isinstance(p.get("target"), dict) and \
                rid != self.derived("overlay", [p["effect"], p["target"]["kind"], p["target"]["id"]]):
            broken.add("derived_id")
        return broken

    def code_ok(self, code: str, m: dict) -> bool:
        spec = self.registry["codes"].get(code)
        if spec is None:
            return True
        kind = spec["value"]
        if (kind == "quantity") != ("unit" in m):
            return False
        numbers = [m[k] for k in ("value", "min", "max") if k in m]
        if kind in ("quantity", "integer", "number"):
            if not all(_number(x) for x in numbers):
                return False
            if kind == "integer" and any(x != int(x) for x in numbers):
                return False
        if kind == "text" and not (isinstance(m.get("value"), str) and re.fullmatch(spec["pattern"], m["value"])):
            return False
        if kind == "category" and not isinstance(m.get("value"), str):
            return False
        factor = spec["units"].get(m["unit"]) if kind == "quantity" else 1
        if factor is None or kind in ("text", "category"):
            return True
        for x in (n * factor for n in numbers):
            if x < spec.get("minimum", x) or x > spec.get("maximum", x) or ("exclusive_minimum" in spec and x <= spec["exclusive_minimum"]):
                return False
        return True

    def refusals(self, kind: str, p: dict) -> list[str]:
        """Why a command won't write this payload: codes, units and values outside the registry, or a code on a
        kind it isn't for. Delivery still keeps them; only writers are held to the registry."""
        out = []
        for metrics in _measures(kind, p):
            for code, m in metrics.items():
                spec = self.registry["codes"].get(code)
                if spec is None:
                    if not code.startswith("x."):
                        out.append(f"unknown code {code}; use a registry code or your own x. one")
                    continue
                if kind != "overlay" and kind not in spec["on"]:
                    out.append(f"{code} isn't used on {kind}")
                if "unit" in m and m["unit"] not in spec.get("units", {}):
                    units = ", ".join(spec.get("units", {})) or "none"
                    out.append(f"{code} has no unit {m['unit']}; use one of {units}")
                if spec["value"] == "category" and m.get("value") not in spec["values"]:
                    out.append(f"{code} has no value {m.get('value')}; use one of {', '.join(spec['values'])}")
        return out

    def canonical(self, code: str, m: dict, key: str = "value") -> float | None:
        """`m[key]` in the code's own unit, or None when it can't be converted."""
        spec, x = self.registry["codes"].get(code), m.get(key)
        if spec is None or not _number(x):
            return None
        if spec["value"] != "quantity":
            return x if spec["value"] in ("integer", "number") else None
        factor = spec["units"].get(m.get("unit"))
        return None if factor is None else x * factor

    def fingerprint(self) -> str:
        """What decides which events this contract projects, and how: the whole registry."""
        return hashlib.sha256(jcs(self.registry).encode()).hexdigest()

    def newer(self, version: int, kinds: dict[str, dict] | None = None) -> Contract:
        """A contract as an install `version` would know it: every kind at that version, plus `kinds`.

        Only for tests and tooling: a real upgrade ships its own schemas.
        """
        registry = copy.deepcopy(self.registry)
        for spec in registry["kinds"].values():
            spec["version"] = max(spec["version"], version)
        registry["kinds"].update(kinds or {})
        return Contract(registry, self.schemas)


PLACEHOLDER_ID = "00000000-0000-7000-8000-000000000000"


def later(event: dict) -> bool:
    """Whether the event is of a later schema_version: kept and relayed, never applied (except an erase)."""
    v = event.get("schema_version")
    return isinstance(v, int) and not isinstance(v, bool) and v > SCHEMA_VERSION


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _measures(kind: str, p: dict):
    if kind in ("session", "effort", "planned_session"):
        yield p.get("metrics") or {}
    if kind == "planned_session":
        for block in p.get("blocks", []):
            yield block.get("metrics") or {}
            for effort in block.get("efforts", []):
                yield effort.get("metrics") or {}
    if kind == "overlay":
        yield (p.get("fields") or {}).get("metrics") or {}
    if kind == "observation" and "code" in p:
        yield {p["code"]: {k: p[k] for k in ("value", "min", "max", "unit") if k in p}}


@lru_cache(maxsize=1)
def _shipped() -> Contract:
    return Contract(documents()[1])


def mac(key: bytes, payload: Any) -> str:
    return hmac.new(key, jcs(payload).encode(), hashlib.sha256).hexdigest()
