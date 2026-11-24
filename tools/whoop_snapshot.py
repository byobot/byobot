#!/usr/bin/env python3
"""Snapshot the parts of WHOOP's API that byobot reads.

    python tools/whoop_snapshot.py path/to/whoop-openapi.json

Takes WHOOP's OpenAPI document (as JSON) and writes the response schemas byobot
validates against. Enums become plain types and optional fields may be null, so a
new sport or score state doesn't break imports; a renamed or retyped field does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "python" / "src" / "byobot" / "whoop.schema.json"
RESPONSES = ("RecoveryCollection", "PaginatedSleepResponse", "PaginatedCycleResponse", "WorkoutCollection")
DROP = {"title", "description", "example", "examples", "default", "format", "externalDocs"}


def nullable(schema: dict) -> dict:
    if isinstance(schema.get("type"), str) and "$ref" not in schema:
        return {**schema, "type": [schema["type"], "null"]}
    return {"anyOf": [schema, {"type": "null"}]}


def clean(node, needed: set):
    if isinstance(node, list):
        return [clean(n, needed) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        needed.add(node["$ref"].rsplit("/", 1)[1])
        return {"$ref": node["$ref"]}
    out = {k: clean(v, needed) for k, v in node.items() if k not in DROP}
    if "enum" in out:
        kinds = {type(v).__name__ for v in out.pop("enum")}
        out.setdefault("type", "string" if kinds == {"str"} else ["string", "number", "integer"])
    if isinstance(out.get("properties"), dict):
        required = set(out.get("required", []))
        out["properties"] = {k: v if k in required else nullable(v) for k, v in out["properties"].items()}
    return out


def main() -> None:
    doc = json.loads(Path(sys.argv[1]).read_text())
    source = doc["components"]["schemas"]
    schemas: dict = {}
    queue = list(RESPONSES)
    while queue:
        name = queue.pop()
        if name in schemas:
            continue
        needed: set = set()
        schemas[name] = clean(source[name], needed)
        queue += sorted(needed - schemas.keys())
    out = {"x-whoop-api-version": doc["info"]["version"], "responses": list(RESPONSES),
           "components": {"schemas": dict(sorted(schemas.items()))}}
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
