#!/usr/bin/env python3
"""Generate the app's Swift types from the byobot JSON Schema contract.

    python tools/gen_swift.py            # write apps/ios/ByobotKit/Sources/ByobotKit/Generated/*.swift
    python tools/gen_swift.py --check    # fail if a committed file is out of date (CI runs this)

schema/v1 is the source of truth; the Swift file is never edited by hand. Record kinds become payload
structs (a header kind gets the header's fields it allows), the envelope and sync messages become structs,
and registry.json becomes the code registry, kind versions and id namespaces. library/activities becomes the
library the app ships.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "v1"
OUT = ROOT / "apps" / "ios" / "ByobotKit" / "Sources" / "ByobotKit" / "Generated" / "Contract.swift"
LIBRARY = ROOT / "library" / "activities"
LIBRARY_OUT = OUT.with_name("Library.swift")

FILES = {"common": "common.schema.json", "records": "records.schema.json", "event": "event.schema.json",
         "sync": "sync.schema.json"}
BY_FILE = {v: k for k, v in FILES.items()}

# Definitions that become Swift types. `None` is a schema's root.
TYPES: dict[tuple[str, str | None], str] = {
    ("common", "ref"): "Ref",
    ("common", "time"): "RecordTime",
    ("records", "side"): "Side",
    ("records", "activity"): "ActivityPayload",
    ("records", "location"): "LocationPayload",
    ("records", "plan"): "PlanPayload",
    ("records", "planned_effort"): "PlannedEffort",
    ("records", "block"): "Block",
    ("records", "planned_session"): "PlannedSessionPayload",
    ("records", "session"): "SessionPayload",
    ("records", "effort"): "EffortPayload",
    ("records", "observation"): "ObservationPayload",
    ("records", "overlay"): "OverlayPayload",
    ("records", "setting"): "SettingPayload",
    ("event", None): "Event",
    ("event", "actor"): "Actor",
    ("sync", "server"): "SyncServer",
    ("sync", "device"): "SyncDevice",
    ("sync", "error"): "SyncError",
    ("sync", "hello"): "SyncHello",
    ("sync", "pair_request"): "PairRequest",
    ("sync", "pair_response"): "PairResponse",
    ("sync", "status"): "SyncStatus",
    ("sync", "events_request"): "EventsRequest",
    ("sync", "event_result"): "EventResult",
    ("sync", "events_response"): "EventsResponse",
    ("sync", "sample"): "Sample",
    ("sync", "deletion"): "Deletion",
    ("sync", "samples_request"): "SamplesRequest",
    ("sync", "samples_response"): "SamplesResponse",
    ("sync", "erased_record"): "ErasedRecord",
    ("sync", "removed_record"): "RemovedRecord",
    ("sync", "rejection"): "Rejection",
    ("sync", "version"): "ConflictVersion",
}

# Enums and objects declared inline on a property: (file, definition, property) -> Swift name.
INLINE = {
    ("records", "observation", "modality"): "Modality",
    ("records", "header", "status"): "RecordStatus",
    ("records", "header", "origin"): "Origin",
    ("records", "header", "links"): "Links",
    ("records", "header", "attachments"): "Attachment",
    ("records", "activity", "targets"): "Targets",
    ("records", "activity", "resistance_curve"): "ResistanceCurve",
    ("records", "overlay", "effect"): "OverlayEffect",
    ("records", "overlay", "fields"): "CorrectedFields",
    ("records", "overlay", "swaps"): "Swap",
    ("event", None, "record"): "RecordRef",
    ("event", None, "op"): "Op",
    ("event", "actor", "kind"): "ActorKind",
    ("sync", "event_result", "status"): "EventStatus",
}

# Types whose `id` is not an identity (a reference's id names another record).
NOT_IDENTIFIABLE = {"Actor", "Ref", "RecordRef", "ErasedRecord"}

STRING_DEFS = {"id", "slug", "activity_id", "uuid", "kind", "timestamp", "date", "zone", "hlc", "text", "name", "code", "unit"}

KEYWORDS = {
    "associatedtype", "class", "deinit", "enum", "extension", "fileprivate", "func", "import", "init", "inout",
    "internal", "let", "open", "operator", "private", "protocol", "public", "rethrows", "static", "struct",
    "subscript", "typealias", "var", "break", "case", "continue", "default", "defer", "do", "else", "fallthrough",
    "for", "guard", "if", "in", "repeat", "return", "switch", "where", "while", "as", "catch", "false", "is", "nil",
    "self", "Self", "super", "throw", "throws", "true", "try", "Any", "some", "any",
}


def load() -> dict[str, dict]:
    schemas = {name: json.loads((SCHEMA / file).read_text(encoding="utf-8")) for name, file in FILES.items()}
    schemas["registry"] = json.loads((SCHEMA / "registry.json").read_text(encoding="utf-8"))
    return schemas


def camel(text: str, upper: bool = False) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", text) if p]
    out = "".join(p[0].upper() + p[1:] for p in parts)
    return out if upper else out[0].lower() + out[1:]


def ident(name: str) -> str:
    return f"`{name}`" if name in KEYWORDS else name


def swift_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def doc(text: str | None, indent: str = "") -> list[str]:
    if not text:
        return []
    return [f"{indent}/// {line}".rstrip() for line in text.splitlines()]


def number(x) -> str:
    return "nil" if x is None else repr(float(x))


class Generator:
    def __init__(self, schemas: dict[str, dict]):
        self.schemas = schemas
        self.enums: dict[str, tuple[list[str], str | None]] = {}
        self.inline: dict[str, list[str]] = {}

    def resolve(self, ref: str, here: str) -> tuple[str, str | None]:
        file, _, pointer = ref.partition("#")
        name = BY_FILE[file] if file else here
        if not pointer:
            return name, None
        match = re.fullmatch(r"/\$defs/([A-Za-z0-9_]+)", pointer)
        if not match:
            raise ValueError(f"unsupported $ref {ref!r} in {here}")
        return name, match.group(1)

    def node(self, file: str, definition: str | None) -> dict:
        schema = self.schemas[file]
        return schema if definition is None else schema["$defs"][definition]

    def properties(self, file: str, definition: str | None, node: dict) -> list[tuple[str, object, tuple, bool]]:
        """(name, schema, (file, definition) it is declared in, required) for each property. A kind built on
        the header gets the header's fields, typed as the header types them, minus those it rules out."""
        refs = [self.resolve(p["$ref"], file) for p in node.get("allOf", []) if "$ref" in p]
        header = ("records", "header") in refs and ("records", "no_header") not in refs
        excluded = {r for part in node.get("not", {}).get("anyOf", []) for r in part.get("required", [])}
        required = set(node.get("required", []))
        out: dict[str, tuple[object, tuple]] = {}
        if header:
            for name, prop in self.node("records", "header")["properties"].items():
                out[name] = (prop, ("records", "header"))
        for name, prop in node.get("properties", {}).items():
            if prop is False:
                out.pop(name, None)
            elif not (header and name in out):
                out[name] = (prop, (file, definition))
        return [(n, p, owner, n in required) for n, (p, owner) in out.items() if n not in excluded]

    def swift_type(self, prop, owner: tuple, name: str) -> tuple[str, bool]:
        """(Swift type, nullable) for one property schema."""
        file = owner[0]
        if prop is True:
            return "JSONValue", False
        if "$ref" in prop:
            rfile, rdef = self.resolve(prop["$ref"], file)
            if rfile == "common" and rdef in STRING_DEFS:
                return "String", False
            if (rfile, rdef) == ("common", "metrics"):
                return "Metrics", False
            if (rfile, rdef) == ("common", "measure"):
                return "Measure", False
            if (rfile, rdef) == ("sync", "kind_versions"):
                return "[String: Int]", False
            if (rfile, rdef) == ("sync", "capabilities"):
                return "[String]", False
            if (rfile, rdef) not in TYPES:
                raise ValueError(f"no Swift type for {rfile}#{rdef}")
            return TYPES[(rfile, rdef)], False
        if "const" in prop:
            c = prop["const"]
            return ("Bool" if isinstance(c, bool) else "Int" if isinstance(c, int) else "String"), False
        key = (*owner, name)
        if "enum" in prop:
            if key not in INLINE:
                raise ValueError(f"name the inline enum {key} in INLINE")
            self.enums[INLINE[key]] = (prop["enum"], prop.get("description"))
            return INLINE[key], False
        types = prop.get("type")
        if isinstance(types, list):
            nullable = "null" in types
            rest = sorted(t for t in types if t != "null")
            if len(rest) == 1:
                return self.swift_type({**prop, "type": rest[0]}, owner, name)[0], nullable
            if set(rest) <= {"number", "string", "boolean"}:
                return "Scalar", nullable
            raise ValueError(f"unsupported type list {types} for {key}")
        if types == "string":
            return "String", False
        if types == "integer":
            return "Int", False
        if types == "number":
            return "Double", False
        if types == "boolean":
            return "Bool", False
        if types == "array":
            items = prop.get("items")
            return (f"[{self.swift_type(items, owner, name)[0]}]" if items is not None else "[JSONValue]"), False
        if types == "object":
            extra = prop.get("additionalProperties")
            if isinstance(extra, dict):
                inner = self.inline_struct(extra, owner, name) if "properties" in extra else \
                    self.swift_type(extra, owner, name)[0]
                return f"[String: {inner}]", False
            if "properties" in prop:
                return self.inline_struct(prop, owner, name), False
        return "JSONValue", False

    def inline_struct(self, prop: dict, owner: tuple, name: str) -> str:
        key = (*owner, name)
        if key not in INLINE:
            raise ValueError(f"name the inline object {key} in INLINE")
        swift = INLINE[key]
        if swift not in self.inline:
            self.inline[swift] = []  # reserve before recursing
            self.inline[swift] = self.struct(owner[0], owner[1], swift, prop)
        return swift

    def struct(self, file: str, definition: str | None, name: str, node: dict) -> list[str]:
        fields = []
        for pname, prop, owner, required in self.properties(file, definition, node):
            swift, nullable = self.swift_type(prop, owner, pname)
            optional = nullable or not required
            desc = prop.get("description") if isinstance(prop, dict) else None
            fields.append((pname, ident(camel(pname)), swift + ("?" if optional else ""), optional, desc))
        has_id = name not in NOT_IDENTIFIABLE and any(p == "id" and t == "String" for p, _, t, _, _ in fields)
        conformances = "Codable, Equatable, Hashable, Sendable" + (", Identifiable" if has_id else "")
        lines = doc(node.get("description"))
        lines.append(f"public struct {name}: {conformances} {{")
        for _, sname, stype, _, desc in fields:
            lines += doc(desc, "    ")
            lines.append(f"    public var {sname}: {stype}")
        lines.append("")
        params = ", ".join(f"{s}: {t}" + (" = nil" if opt else "") for _, s, t, opt, _ in fields)
        lines.append(f"    public init({params}) {{")
        for _, sname, _, _, _ in fields:
            lines.append(f"        self.{sname.strip('`')} = {sname}")
        lines += ["    }", "", "    enum CodingKeys: String, CodingKey {"]
        for pname, sname, _, _, _ in fields:
            lines.append(f"        case {sname}" + (f' = "{pname}"' if sname.strip("`") != pname else ""))
        lines += ["    }", "}"]
        return lines

    def enum(self, name: str, values: list[str], description: str | None) -> list[str]:
        lines = doc(description)
        lines.append(f"public enum {name}: Codable, CaseIterable, Hashable, Sendable {{")
        cases = [(ident(camel(raw)), raw) for raw in values]
        lines += [f"    case {case}" for case, _ in cases]
        lines += ["    /// A value from a newer byobot, kept as written so it goes back unchanged.", "    case other(String)", "",
                  f"    public static let allCases: [{name}] = [{', '.join(f'.{c}' for c, _ in cases)}]", "",
                  "    public init(rawValue: String) {", "        switch rawValue {"]
        lines += [f"        case {swift_string(raw)}: self = .{case}" for case, raw in cases]
        lines += ["        default: self = .other(rawValue)", "        }", "    }", "",
                  "    public var rawValue: String {", "        switch self {"]
        lines += [f"        case .{case}: return {swift_string(raw)}" for case, raw in cases]
        lines += ["        case .other(let value): return value", "        }", "    }", "",
                  "    public init(from decoder: any Decoder) throws {",
                  "        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))", "    }", "",
                  "    public func encode(to encoder: any Encoder) throws {",
                  "        var container = encoder.singleValueContainer()",
                  "        try container.encode(rawValue)", "    }", "}"]
        return lines

    def registry(self) -> list[str]:
        """Kind versions, id namespaces, settings and the code registry from registry.json."""
        reg = self.schemas["registry"]
        code = self.schemas["common"]["$defs"]["code"]["pattern"]
        lines = ["/// The payload version each kind is written at.", "public enum KindVersion {",
                 "    public static let all: [String: Int] = ["]
        lines += [f"        {json.dumps(k)}: {v['version']}," for k, v in reg["kinds"].items()]
        lines += ["    ]", "}", "", "/// Namespaces for ids derived from other ids (UUIDv5).", "public enum Namespace {"]
        lines += [f"    public static let {camel(k)} = {json.dumps(v)}" for k, v in reg["namespaces"].items()]
        lines += ["}", "", "/// Settings this contract knows, by record id.",
                  "public enum SettingId: String, CaseIterable, Sendable {"]
        lines += [f"    case {camel(k)}" + (f' = "{k}"' if camel(k) != k else "") for k in reg["settings"]]
        lines += ["}", "", "/// The registry's codes: what each measures, its units (the first is its own) and bounds.",
                  "/// Anything else is your own code, prefixed `x.`.", "public enum MetricRegistry {",
                  f"    public static let codePattern = {swift_string(code)}", "",
                  "    public static let all: [MetricSpec] = ["]
        for name, spec in reg["codes"].items():
            value = spec["value"]
            kind = {"quantity": "number", "category": "text"}.get(value, value)
            units = ", ".join(swift_string(u) for u in spec.get("units", {}))
            factors = {u: f for u, f in spec.get("units", {}).items() if f is not None}
            factors = "[" + (", ".join(f"{swift_string(u)}: {number(f)}" for u, f in factors.items()) or ":") + "]"
            values = "nil" if "values" not in spec else "[" + ", ".join(swift_string(v) for v in spec["values"]) + "]"
            minimum = spec.get("minimum", spec.get("exclusive_minimum"))
            lines.append(
                f"        MetricSpec(name: {json.dumps(name)}, summary: {swift_string(spec.get('description', ''))}, "
                f"units: [{units}], factors: {factors}, unitRequired: {'true' if value == 'quantity' else 'false'}, kind: .{kind}, "
                f"minimum: {number(minimum)}, exclusiveMinimum: {'true' if 'exclusive_minimum' in spec else 'false'}, "
                f"maximum: {number(spec.get('maximum'))}, "
                f"pattern: {'nil' if 'pattern' not in spec else swift_string(spec['pattern'])}, values: {values}, "
                f"on: [{', '.join(swift_string(k) for k in spec.get('on', []))}]),")
        lines += ["    ]", "", "    /// The code each HealthKit type is read as.", "    public static let healthkit: [String: String] = ["]
        lines += [f"        {json.dumps(spec['healthkit'])}: {json.dumps(name)}," for name, spec in reg["codes"].items()
                  if "healthkit" in spec]
        lines += ["    ]", "}"]
        return lines

    def generate(self) -> str:
        blocks: list[list[str]] = []
        for (file, definition), name in TYPES.items():
            node = self.node(file, definition)
            if "enum" in node:
                self.enums[name] = (node["enum"], node.get("description"))
            else:
                blocks.append(self.struct(file, definition, name, node))
        blocks += [self.inline[n] for n in sorted(self.inline)]
        blocks.append(self.registry())
        enums = [self.enum(n, v, d) for n, (v, d) in sorted(self.enums.items())]
        header = [
            "// Generated by tools/gen_swift.py from schema/v1. Do not edit by hand:",
            "// change the schema and run `python tools/gen_swift.py`.",
            "//",
            "// Copyright The byobot Authors. Licensed under Apache-2.0.",
            "",
            "import Foundation",
            "",
            "public enum ContractVersion {",
            f"    public static let schema = {self.schemas['event']['properties']['schema_version']['const']}",
            f"    public static let sync = {self.schemas['sync']['$defs']['hello']['properties']['protocol']['const']}",
            "}",
            "",
            "/// Measurements by code (`reps`, `load`, ..., or your own `x.name`).",
            "public typealias Metrics = [String: Measure]",
        ]
        return "\n\n".join("\n".join(b) for b in (header, *enums, *blocks)) + "\n"


def library() -> str:
    """The library's activities as plan entries, one per line, so the plan sends only those it names."""
    entries = [json.dumps({"id": f.stem, "payload": json.loads(f.read_text(encoding="utf-8")), "library": True},
                          ensure_ascii=False, separators=(",", ":")) for f in sorted(LIBRARY.glob("*.json"))]
    return "\n".join([
        "// Generated by tools/gen_swift.py from library/activities. Do not edit by hand:",
        "// change the library and run `python tools/gen_swift.py`.",
        "//",
        "// Copyright The byobot Authors. Licensed under Apache-2.0.",
        "",
        "extension ActivityLibrary {",
        "    /// The activities byobot shipped with this app. The plan sends those it names, newer ones included.",
        "    public static let bundled: [Activity] = decode(#\"\"\"",
        "    [",
        *(f"    {e}," for e in entries[:-1]), *(f"    {e}" for e in entries[-1:]),
        "    ]",
        "    \"\"\"#)",
        "}",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="Fail if a generated file is out of date.")
    args = parser.parse_args()
    for out, text in ((OUT, Generator(load()).generate()), (LIBRARY_OUT, library())):
        if args.check:
            if (out.read_text(encoding="utf-8") if out.exists() else "") != text:
                sys.stderr.write(f"{out.relative_to(ROOT)} is out of date; run `python tools/gen_swift.py`\n")
                return 1
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
