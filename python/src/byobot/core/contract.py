"""The schema/v1 contract: validating events, the rules beyond the schemas, derived ids and outcomes."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry, Resource

SCHEMA_VERSION = 1
SCHEMA_NAMES = ("common", "records", "event", "registry")
SLUG_KINDS = ("activity", "location", "setting")
RULES = ("time-order", "date-order", "range-order", "code-value", "part-ids", "self-reference", "derived-id")


def schema_dir() -> Path:
    here = Path(__file__).resolve().parent.parent
    for candidate in (here / "_schema" / "v1", here.parents[2] / "schema" / "v1", here.parents[1] / "schema" / "v1"):
        if (candidate / "event.schema.json").is_file():
            return candidate
    raise RuntimeError("byobot schema/v1 files not found; reinstall the package")


@lru_cache(maxsize=1)
def _documents() -> tuple[dict[str, dict], dict]:
    docs = {n: json.loads((schema_dir() / f"{n}.schema.json").read_text(encoding="utf-8")) for n in SCHEMA_NAMES}
    return docs, json.loads((schema_dir() / "registry.json").read_text(encoding="utf-8"))


def jcs(value: Any) -> str:
    """RFC 8785 canonical JSON for the values byobot writes: sorted keys, no whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def instant(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class Rejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Contract:
    """The schemas and registry one install knows. `registry` may be newer than the shipped one."""

    registry: dict
    schemas: dict[str, dict] = field(default_factory=lambda: _documents()[0])

    @classmethod
    def shipped(cls) -> "Contract":
        return _shipped()

    @cached_property
    def _validator(self) -> Draft202012Validator:
        resources = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in self.schemas.values())
        return Draft202012Validator(self.schemas["event"], registry=resources)

    def version(self, kind: str) -> int | None:
        known = self.registry["kinds"].get(kind)
        return known and known["version"]

    def namespace(self, name: str) -> uuid.UUID:
        return uuid.UUID(self.registry["namespaces"][name])

    def derived(self, namespace: str, parts: list) -> str:
        return str(uuid.uuid5(self.namespace(namespace), jcs(parts)))

    def check(self, event: dict) -> None:
        """Raise Rejected if the event breaks the schemas or a rule."""
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

    def understands(self, event: dict) -> bool:
        """Whether this install can project the event (it is stored either way)."""
        kind, op = event["record"]["kind"], event["op"]
        known = self.version(kind)
        if known is None or (op == "put" and event["kind_version"] > known):
            return False
        return not (kind == "setting" and op == "put" and event["record"]["id"] not in self.registry["settings"])

    def broken_rules(self, event: dict) -> set[str]:
        broken = set()
        if event["id"] in event["prev"] or event.get("reverts") == event["id"]:
            broken.add("self-reference")
        if event["op"] != "put" or "redacted_by" in event:
            return broken
        kind, rid, p = event["record"]["kind"], event["record"]["id"], event["payload"]
        for t in (p.get("time"), (p.get("fields") or {}).get("time")):
            if isinstance(t, dict) and "end" in t and "start" in t and instant(t["end"]) < instant(t["start"]):
                broken.add("time-order")
        if kind == "plan" and "ends_on" in p and p["ends_on"] < p["starts_on"]:
            broken.add("date-order")
        for metrics in _measures(kind, p):
            for code, m in metrics.items():
                if "min" in m and "max" in m and _number(m["min"]) and _number(m["max"]) and m["min"] > m["max"]:
                    broken.add("range-order")
                if not self.code_ok(code, m):
                    broken.add("code-value")
        if kind == "planned_session":
            parts = [b["id"] for b in p["blocks"]] + [e["id"] for b in p["blocks"] for e in b["efforts"]]
            if len(parts) != len(set(parts)):
                broken.add("part-ids")
        if kind == "source_record" and {"source", "source_type", "source_id"} <= set(p) and \
                rid != self.derived("source_record", [p["source"], p["source_type"], p["source_id"]]):
            broken.add("derived-id")
        if kind == "overlay" and "effect" in p and isinstance(p.get("target"), dict) and \
                rid != self.derived("overlay", [p["effect"], p["target"]["kind"], p["target"]["id"]]):
            broken.add("derived-id")
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
            if x < spec.get("minimum", x) or x > spec.get("maximum", x) or x <= spec.get("exclusive_minimum", x - 1):
                return False
        return True

    def fingerprint(self) -> str:
        """What decides which events this contract projects."""
        return jcs([{k: v["version"] for k, v in self.registry["kinds"].items()}, sorted(self.registry["settings"])])

    def newer(self, version: int, kinds: dict[str, dict] | None = None) -> "Contract":
        """A contract as an install `version` would know it: every kind at that version, plus `kinds`.

        Only for tests and tooling: a real upgrade ships its own schemas.
        """
        registry = copy.deepcopy(self.registry)
        for spec in registry["kinds"].values():
            spec["version"] = max(spec["version"], version)
        registry["kinds"].update(kinds or {})
        return Contract(registry, self.schemas)


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
        yield {p["code"]: {k: p[k] for k in ("value", "unit") if k in p}}


@lru_cache(maxsize=1)
def _shipped() -> Contract:
    return Contract(_documents()[1])


def mac(key: bytes, payload: Any) -> str:
    return hmac.new(key, jcs(payload).encode(), hashlib.sha256).hexdigest()
