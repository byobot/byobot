#!/usr/bin/env python3
"""Generate the app's Swift types from the byobot JSON Schema contract.

    python tools/gen_swift.py            # write apps/ios/ByobotKit/Sources/ByobotKit/Generated/Contract.swift
    python tools/gen_swift.py --check    # fail if the committed file is out of date (CI runs this)

The schemas in schema/v0 are the source of truth; the Swift file is never edited
by hand. The generator covers exactly the shapes the contract uses: objects with
properties (structs), string enums, `$ref`s across the schema files, and the
event envelope's payload-by-type (an enum). Metrics are a map of `Measure`,
which lives in the hand-written support code beside the generated file, and the
metric registry (names, units, bounds) is generated so the app can offer the
right units without hard-coding them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema" / "v0"
OUT = ROOT / "apps" / "ios" / "ByobotKit" / "Sources" / "ByobotKit" / "Generated" / "Contract.swift"

FILES = {"common": "common.schema.json", "event": "event.schema.json", "training": "training.schema.json",
         "telemetry": "telemetry.schema.json", "sync": "sync.schema.json"}
BY_FILE = {v: k for k, v in FILES.items()}

# Which definitions become Swift types, and their names. `None` is the schema's root object.
TYPES: dict[tuple[str, str | None], str] = {
    ("training", "activity"): "Activity",
    ("training", "targets"): "Targets",
    ("training", "resistance_curve"): "ResistanceCurve",
    ("training", "location"): "Location",
    ("training", "plan"): "Plan",
    ("training", "side"): "Side",
    ("training", "planned_effort"): "PlannedEffort",
    ("training", "block"): "Block",
    ("training", "planned_session"): "PlannedSession",
    ("training", "performed_effort"): "PerformedEffort",
    ("training", "inline_effort"): "InlineEffort",
    ("training", "performed_session"): "PerformedSession",
    ("event", None): "Event",
    ("event", "actor"): "Actor",
    ("event", "undo"): "Undo",
    ("telemetry", None): "TelemetrySample",
    ("telemetry", "device"): "TelemetryDevice",
    ("sync", "server"): "SyncServer",
    ("sync", "device"): "SyncDevice",
    ("sync", "error"): "SyncError",
    ("sync", "hello"): "SyncHello",
    ("sync", "pair_request"): "PairRequest",
    ("sync", "pair_response"): "PairResponse",
    ("sync", "status"): "SyncStatus",
    ("sync", "plan_window"): "PlanWindow",
    ("sync", "events_request"): "EventsRequest",
    ("sync", "event_result"): "EventResult",
    ("sync", "events_response"): "EventsResponse",
    ("sync", "telemetry_request"): "TelemetryRequest",
    ("sync", "telemetry_response"): "TelemetryResponse",
}

# Names for enums declared inline on a property: (file, definition, property) -> Swift name.
INLINE_ENUMS = {
    ("event", None, "type"): "EventType",
    ("event", "actor", "kind"): "ActorKind",
    ("telemetry", None, "type"): "TelemetryType",
    ("sync", "event_result", "status"): "EventStatus",
}

# Types whose `id` is not an identity (an actor's id names who, not which event).
NOT_IDENTIFIABLE = {"Actor"}

# Shared scalar definitions that are plain strings in Swift (the contract checks their format).
STRING_DEFS = {"id", "slug", "uuid", "timestamp", "date", "text", "metric_name", "custom_metric_name"}

KEYWORDS = {
    "associatedtype", "class", "deinit", "enum", "extension", "fileprivate", "func", "import", "init", "inout",
    "internal", "let", "open", "operator", "private", "protocol", "public", "rethrows", "static", "struct",
    "subscript", "typealias", "var", "break", "case", "continue", "default", "defer", "do", "else", "fallthrough",
    "for", "guard", "if", "in", "repeat", "return", "switch", "where", "while", "as", "catch", "false", "is", "nil",
    "self", "Self", "super", "throw", "throws", "true", "try", "Any", "some", "any",
}

PREFIXES = ("HKQuantityTypeIdentifier", "HKCategoryTypeIdentifier", "byobot.")
NAMES = {"HKWorkoutTypeIdentifier": "workout", "HKQuantityTypeIdentifierVO2Max": "vo2Max"}


def load() -> dict[str, dict]:
    return {name: json.loads((SCHEMA / file).read_text(encoding="utf-8")) for name, file in FILES.items()}


def camel(text: str, upper: bool = False) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", text) if p]
    out = "".join(p[0].upper() + p[1:] for p in parts)
    return out if upper else out[0].lower() + out[1:]


def ident(name: str) -> str:
    return f"`{name}`" if name in KEYWORDS else name


def case_name(raw: str) -> str:
    if raw in NAMES:
        return NAMES[raw]
    for prefix in PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return ident(camel(raw))


def swift_string(text: str) -> str:
    """A Swift string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def doc(text: str | None, indent: str = "") -> list[str]:
    if not text:
        return []
    return [f"{indent}/// {line}".rstrip() for line in text.splitlines()]


class Generator:
    def __init__(self, schemas: dict[str, dict]):
        self.schemas = schemas
        self.enums: dict[str, tuple[list[str], str | None]] = {}  # name -> (raw values, doc)

    # -- references --------------------------------------------------------

    def resolve(self, ref: str, here: str) -> tuple[str, str | None]:
        """A `$ref` as (file, definition); definition is None for a schema's root."""
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

    def properties(self, file: str, definition: str | None) -> list[tuple[str, dict, str, bool]]:
        """(name, schema, file it came from, required) for each property, following `allOf` composition."""
        node = self.node(file, definition)
        required = set(node.get("required", []))
        base: dict[str, tuple[dict, str]] = {}
        for part in node.get("allOf", []):
            if "$ref" in part:
                bfile, bdef = self.resolve(part["$ref"], file)
                bnode = self.node(bfile, bdef)
                for name, prop in bnode.get("properties", {}).items():
                    base[name] = (prop, bfile)
                required |= set(bnode.get("required", []))
        out = []
        for name, prop in node.get("properties", {}).items():
            source = file
            if prop is True:
                prop, source = base[name]
            out.append((name, prop, source, name in required))
        return out

    # -- types -------------------------------------------------------------

    def swift_type(self, prop: dict, file: str, owner: tuple[str, str | None], name: str) -> tuple[str, bool]:
        """(Swift type, nullable) for one property schema."""
        if "$ref" in prop:
            rfile, rdef = self.resolve(prop["$ref"], file)
            if rfile == "common":
                if rdef in STRING_DEFS:
                    return "String", False
                if rdef == "metrics":
                    return "Metrics", False
                if rdef == "measure":
                    return "Measure", False
                raise ValueError(f"no Swift type for common#{rdef}")
            if (rfile, rdef) not in TYPES:
                raise ValueError(f"no Swift type for {rfile}#{rdef}")
            return TYPES[(rfile, rdef)], False
        if "const" in prop:
            return ("Int" if isinstance(prop["const"], int) else "String"), False
        if "enum" in prop:
            key = (owner[0], owner[1], name)
            if key not in INLINE_ENUMS:
                raise ValueError(f"name the inline enum {key} in INLINE_ENUMS")
            self.enums[INLINE_ENUMS[key]] = (prop["enum"], prop.get("description"))
            return INLINE_ENUMS[key], False
        types = prop.get("type")
        if isinstance(types, list):
            nullable = "null" in types
            rest = sorted(t for t in types if t != "null")
            if len(rest) == 1:
                inner, _ = self.swift_type({**prop, "type": rest[0]}, file, owner, name)
                return inner, nullable
            if set(rest) <= {"number", "string", "boolean"}:
                return "Scalar", nullable
            raise ValueError(f"unsupported type list {types} for {owner}.{name}")
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
            if items is None:
                return "[JSONValue]", False
            inner, _ = self.swift_type(items, file, owner, name)
            return f"[{inner}]", False
        if types == "object":
            extra = prop.get("additionalProperties")
            if isinstance(extra, dict):
                inner, _ = self.swift_type(extra, file, owner, name)
                return f"[String: {inner}]", False
            return "JSONValue", False
        return "JSONValue", False  # anything goes (`{}` or a description only)

    # -- emitters ----------------------------------------------------------

    def struct(self, file: str, definition: str | None, name: str) -> list[str]:
        node = self.node(file, definition)
        props = self.properties(file, definition)
        fields = []
        for pname, prop, source, required in props:
            swift, nullable = self.swift_type(prop, source, (file, definition), pname)
            optional = nullable or not required
            fields.append((pname, ident(camel(pname)), swift + ("?" if optional else ""), optional,
                           prop.get("description")))
        has_id = name not in NOT_IDENTIFIABLE and any(p == "id" and t == "String" for p, _, t, _, _ in fields)
        conformances = "Codable, Equatable, Hashable, Sendable" + (", Identifiable" if has_id else "")
        lines = doc(node.get("description") or node.get("title"))
        lines.append(f"public struct {name}: {conformances} {{")
        for pname, sname, stype, _, desc in fields:
            lines += doc(desc, "    ")
            lines.append(f"    public var {sname}: {stype}")
        lines.append("")
        params = ", ".join(f"{s}: {t}" + (" = nil" if opt else "") for _, s, t, opt, _ in fields)
        lines.append(f"    public init({params}) {{")
        for _, sname, _, _, _ in fields:
            lines.append(f"        self.{sname.strip('`')} = {sname}")
        lines.append("    }")
        lines.append("")
        lines.append("    enum CodingKeys: String, CodingKey {")
        for pname, sname, _, _, _ in fields:
            lines.append(f"        case {sname}" + (f' = "{pname}"' if sname.strip("`") != pname else ""))
        lines.append("    }")
        lines.append("}")
        return lines

    def enum(self, name: str, values: list[str], description: str | None) -> list[str]:
        lines = doc(description)
        lines.append(f"public enum {name}: String, Codable, CaseIterable, Hashable, Sendable {{")
        for raw in values:
            case = case_name(raw)
            lines.append(f"    case {case}" + (f' = "{raw}"' if case.strip("`") != raw else ""))
        lines.append("}")
        return lines

    def event(self) -> list[str]:
        """The envelope: like any struct, but its payload's type depends on `type`."""
        schema = self.schemas["event"]
        mapping = []
        for rule in schema["allOf"]:
            kind = rule["if"]["properties"]["type"]["const"]
            pfile, pdef = self.resolve(rule["then"]["properties"]["payload"]["$ref"], "event")
            mapping.append((kind, TYPES[(pfile, pdef)]))
        self.enums["EventType"] = (schema["properties"]["type"]["enum"], "What an event records.")
        lines = ["/// What an event carries, by event type.",
                 "public enum EventPayload: Equatable, Hashable, Sendable {"]
        for kind, swift in mapping:
            lines.append(f"    case {case_name(kind)}({swift})")
        lines += ["", "    public var type: EventType {", "        switch self {"]
        for kind, _ in mapping:
            lines.append(f"        case .{case_name(kind)}: return .{case_name(kind)}")
        lines += ["        }", "    }", "}", ""]
        props = {name: (prop, required) for name, prop, _, required in self.properties("event", None)}
        lines += doc(schema["description"])
        lines += [
            "public struct Event: Codable, Equatable, Hashable, Sendable, Identifiable {",
            *doc(props["id"][0].get("description"), "    "),
            "    public var id: String",
            "    public var schemaVersion: Int",
            *doc(props["ts"][0].get("description"), "    "),
            "    public var ts: String",
            *doc(props["device_id"][0].get("description"), "    "),
            "    public var deviceId: String",
            "    public var actor: Actor",
            "    public var payload: EventPayload",
            "",
            "    public var type: EventType { payload.type }",
            "",
            "    public init(id: String, schemaVersion: Int = ContractVersion.schema, ts: String, deviceId: String,"
            " actor: Actor, payload: EventPayload) {",
            "        self.id = id",
            "        self.schemaVersion = schemaVersion",
            "        self.ts = ts",
            "        self.deviceId = deviceId",
            "        self.actor = actor",
            "        self.payload = payload",
            "    }",
            "",
            "    enum CodingKeys: String, CodingKey {",
            '        case id, type, ts, actor, payload',
            '        case schemaVersion = "schema_version"',
            '        case deviceId = "device_id"',
            "    }",
            "",
            "    public init(from decoder: Decoder) throws {",
            "        let c = try decoder.container(keyedBy: CodingKeys.self)",
            "        id = try c.decode(String.self, forKey: .id)",
            "        schemaVersion = try c.decode(Int.self, forKey: .schemaVersion)",
            "        ts = try c.decode(String.self, forKey: .ts)",
            "        deviceId = try c.decode(String.self, forKey: .deviceId)",
            "        actor = try c.decode(Actor.self, forKey: .actor)",
            "        switch try c.decode(EventType.self, forKey: .type) {",
        ]
        for kind, swift in mapping:
            lines.append(f"        case .{case_name(kind)}: payload = .{case_name(kind)}("
                         f"try c.decode({swift}.self, forKey: .payload))")
        lines += [
            "        }",
            "    }",
            "",
            "    public func encode(to encoder: Encoder) throws {",
            "        var c = encoder.container(keyedBy: CodingKeys.self)",
            "        try c.encode(id, forKey: .id)",
            "        try c.encode(schemaVersion, forKey: .schemaVersion)",
            "        try c.encode(type, forKey: .type)",
            "        try c.encode(ts, forKey: .ts)",
            "        try c.encode(deviceId, forKey: .deviceId)",
            "        try c.encode(actor, forKey: .actor)",
            "        switch payload {",
        ]
        for kind, _ in mapping:
            lines.append(f"        case .{case_name(kind)}(let p): try c.encode(p, forKey: .payload)")
        lines += ["        }", "    }", "}"]
        return lines

    def registry(self) -> list[str]:
        """The metric registry from common.schema.json, so the app offers the right units and bounds."""
        common = self.schemas["common"]["$defs"]
        metrics = common["metrics"]["properties"]
        lines = [
            "/// The registry metrics from the contract: what each measures, its units and bounds.",
            "/// Anything else must be a custom metric named `x.something`.",
            "public enum MetricRegistry {",
            f'    public static let customNamePattern = {swift_string(common["custom_metric_name"]["pattern"])}',
            "",
            "    public static let all: [MetricSpec] = [",
        ]
        for name in common["metric_name"]["anyOf"][0]["enum"]:
            spec = metrics[name]
            props = spec.get("properties", {})
            value = props.get("value", {})
            units = props.get("unit", {}).get("enum", [])
            kind = "text" if value.get("type") == "string" else ("integer" if value.get("type") == "integer" else "number")
            minimum = value.get("minimum")
            maximum = value.get("maximum")
            pattern = value.get("pattern")
            lines.append(
                f"        MetricSpec(name: {json.dumps(name)}, summary: {swift_string(spec.get('description', ''))}, "
                f"units: [{', '.join(swift_string(u) for u in units)}], unitRequired: {'true' if 'unit' in spec.get('required', []) else 'false'}, "
                f"kind: .{kind}, minimum: {'nil' if minimum is None else float(minimum)}, "
                f"maximum: {'nil' if maximum is None else float(maximum)}, "
                f"pattern: {'nil' if pattern is None else swift_string(pattern)}),")
        lines += ["    ]", "}"]
        return lines

    def generate(self) -> str:
        blocks: list[list[str]] = []
        for (file, definition), name in TYPES.items():
            node = self.node(file, definition)
            if file == "event" and definition is None:
                blocks.append(self.event())
            elif "enum" in node:
                self.enums[name] = (node["enum"], node.get("description"))
            else:
                blocks.append(self.struct(file, definition, name))
        blocks.append(self.registry())
        enums = [self.enum(n, v, d) for n, (v, d) in sorted(self.enums.items())]
        header = [
            "// Generated by tools/gen_swift.py from schema/v0. Do not edit by hand:",
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
            "/// What is measured, by metric name (`reps`, `load`, ..., or your own `x.name`).",
            "public typealias Metrics = [String: Measure]",
        ]
        body = "\n\n".join("\n".join(b) for b in (header, *enums, *blocks))
        return body + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="Fail if the generated file is out of date.")
    args = parser.parse_args()
    text = Generator(load()).generate()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            sys.stderr.write(f"{OUT.relative_to(ROOT)} is out of date; run `python tools/gen_swift.py`\n")
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
