"""The canonical snapshot: what an agent could learn from a store, normalized so comparisons are exact.

Shape, shared by every driver and the reference model:

    records    {"kind/id": {"fields": {...}, "by": "actor_kind:actor_id", "on": replica}}
    conflicts  {"kind/id": [version fields, ...]}  sorted, one entry per distinct concurrent version
    erased     ["kind/id", ...]
    derived    {"kind/id": {"local_date": "YYYY-MM-DD", "metrics": {code: standard value}}}  sessions and efforts
    workouts   [occurrence(...) with "provenance": {metric: "you" or source}, ...]  sorted

Arrival order, internal row ids and event ids never appear.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

SECTIONS = ("records", "conflicts", "erased", "derived", "workouts")


def instant_key(text: str) -> str:
    """A timestamp as UTC with nine fraction digits, so plain string order is time order across offsets."""
    match = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", text)
    if not match:
        raise ValueError(f"not a timestamp: {text!r}")
    base, frac, offset = match.groups()
    whole = datetime.fromisoformat(f"{base}{'+00:00' if offset == 'Z' else offset}").astimezone(timezone.utc)
    return f"{whole:%Y-%m-%dT%H:%M:%S}.{(frac or '').ljust(9, '0')}Z"


def canonical(value: Any) -> str:
    """JSON with sorted keys, where 5 and 5.0 are the same number, as they are in JSON."""
    return json.dumps(_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_numbers(v) for v in value]
    return value


def key(kind: str, rid: str) -> str:
    return f"{kind}/{rid}"


def empty() -> dict:
    return {"records": {}, "conflicts": {}, "erased": [], "derived": {}, "workouts": []}


def normalize(snapshot: dict) -> dict:
    out = {s: snapshot.get(s, empty()[s]) for s in SECTIONS}
    out["conflicts"] = {k: sorted(v, key=canonical) for k, v in out["conflicts"].items()}
    out["erased"] = sorted(out["erased"])
    out["workouts"] = sorted(out["workouts"], key=canonical)
    return json.loads(canonical(out))


def equal(a: dict, b: dict) -> bool:
    return canonical(normalize(a)) == canonical(normalize(b))


def diff(a: dict, b: dict) -> list[str]:
    """Readable differences, for assertion messages."""
    a, b = normalize(a), normalize(b)
    out = []
    for section in SECTIONS:
        x, y = a[section], b[section]
        if isinstance(x, dict):
            for k in sorted(set(x) | set(y)):
                if x.get(k) != y.get(k):
                    out.append(f"{section} {k}: {canonical(x.get(k))} != {canonical(y.get(k))}")
        elif x != y:
            out.append(f"{section}: {x} != {y}")
    return out


def num(value: float | int) -> str:
    """A number as an exact decimal string, so 9000 and 9000.0 compare equal."""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def occurrence(start: str, end: str, metrics: dict) -> dict:
    """One workout as reads see it: instants in UTC, metrics as exact decimals."""
    return {"start": instant_key(start), "end": instant_key(end),
            "metrics": {k: num(v) for k, v in metrics.items() if v is not None}}
