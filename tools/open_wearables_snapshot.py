#!/usr/bin/env python3
"""Snapshot the parts of the Open Wearables API that byobot reads.

    python tools/open_wearables_snapshot.py VERSION path/to/openapi.json

Takes `docs/openapi.json` from an Open Wearables release and writes the response
schemas byobot validates against. Enums become plain types, so a new provider or
workout type upstream doesn't break imports; a renamed or retyped field does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "python" / "src" / "byobot" / "open_wearables.schema.json"
RESPONSES = (
    "PaginatedResponse_TimeSeriesSample_",
    "PaginatedResponse_SleepSession_",
    "PaginatedResponse_HealthScoreResponse_",
    "PaginatedResponse_Workout_",
)
DROP = {"title", "description", "example", "examples", "default", "format"}


def clean(node, schemas: dict, needed: set):
    if isinstance(node, list):
        return [clean(n, schemas, needed) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[1]
        if name not in needed:
            needed.add(name)
            schemas[name] = None
        return {"$ref": node["$ref"]}
    out = {k: clean(v, schemas, needed) for k, v in node.items() if k not in DROP}
    if "enum" in out:
        kinds = {type(v).__name__ for v in out.pop("enum")}
        out.setdefault("type", "string" if kinds == {"str"} else ["string", "number", "integer"])
    return out


def main() -> None:
    version, path = sys.argv[1], Path(sys.argv[2])
    source = json.loads(path.read_text())["components"]["schemas"]
    schemas: dict = {}
    needed = set(RESPONSES)
    queue = list(RESPONSES)
    while queue:
        name = queue.pop()
        before = set(needed)
        schemas[name] = clean(source[name], schemas, needed)
        queue += sorted(needed - before)
    doc = {"x-open-wearables-version": version, "responses": list(RESPONSES),
           "components": {"schemas": dict(sorted(schemas.items()))}}
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
