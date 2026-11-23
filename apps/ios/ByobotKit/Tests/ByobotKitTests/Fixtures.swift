import Foundation
import XCTest
@testable import ByobotKit

/// The shared fixtures at the repository root: the same files the Python core tests against.
enum Fixtures {
    static let root: URL = {
        var dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        while dir.path != "/" {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("schema/fixtures/v0").path) {
                return dir
            }
            dir = dir.deletingLastPathComponent()
        }
        fatalError("schema/fixtures/v0 not found above \(#filePath)")
    }()

    static func json(_ path: String) throws -> JSONValue {
        try JSONValue.parse(Data(contentsOf: root.appendingPathComponent(path)))
    }

    static func cases(_ path: String, key: String = "cases") throws -> [[String: JSONValue]] {
        guard case .object(let doc) = try json(path), case .array(let items)? = doc[key] else {
            throw XCTSkip("no \(key) in \(path)")
        }
        return items.compactMap { if case .object(let o) = $0 { return o } else { return nil } }
    }

    static func data(_ value: JSONValue) throws -> Data {
        try JSONEncoder().encode(value)
    }

    /// A pattern from the shared definitions in common.schema.json (id, uuid, timestamp, date, ...).
    static func pattern(_ name: String) throws -> String {
        guard case .object(let doc) = try json("schema/v0/common.schema.json"),
              case .object(let defs)? = doc["$defs"], case .object(let def)? = defs[name],
              case .string(let pattern)? = def["pattern"] else {
            throw XCTSkip("no pattern for \(name)")
        }
        return pattern
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    func string(_ key: String) -> String? {
        if case .string(let s)? = self[key] { return s }
        return nil
    }

    func bool(_ key: String) -> Bool? {
        if case .bool(let b)? = self[key] { return b }
        return nil
    }
}

func matches(_ text: String, _ pattern: String) -> Bool {
    text.range(of: pattern, options: .regularExpression) != nil
}
