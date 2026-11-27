import Foundation
import XCTest
@testable import ByobotKit

/// The shared fixtures at the repository root: the same files the Python core tests against.
enum Fixtures {
    static let root: URL = {
        var dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        while dir.path != "/" {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("schema/fixtures/v1").path) {
                return dir
            }
            dir = dir.deletingLastPathComponent()
        }
        fatalError("schema/fixtures/v1 not found above \(#filePath)")
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

    /// A plan window with a library, a plan and planned sessions of every shape, for the app's own tests. Its laptop
    /// holds everything the test phone sends.
    static func window() throws -> PlanWindow {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("plan-window.json")
        return try JSONDecoder().decode(PlanWindow.self, from: Data(contentsOf: url))
    }

    /// A pattern from the shared definitions in common.schema.json (id, uuid, timestamp, date, ...).
    static func pattern(_ name: String) throws -> String {
        guard case .object(let doc) = try json("schema/v1/common.schema.json"),
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

extension Record where Payload == ActivityPayload {
    init(id: String, name: String, defaultMetrics: [String]? = nil) {
        self.init(id: id, payload: ActivityPayload(name: name, defaultMetrics: defaultMetrics))
    }
}

extension Record where Payload == PlannedSessionPayload {
    init(id: String, scheduledOn: String, name: String, locationId: String? = nil, blocks: [Block]) {
        self.init(id: id, payload: PlannedSessionPayload(scheduledOn: scheduledOn, name: name, locationId: locationId,
                                                         blocks: blocks))
    }
}

extension Record where Payload == EffortPayload {
    /// A set as the laptop sends it.
    init(id: String, activityId: String, performedAt: String, session: String? = nil, planned: String? = nil,
         part: String? = nil, round: Int? = nil, locationId: String? = nil, metrics: Metrics? = nil) {
        let links = Links(partOf: session.map { Ref(kind: "session", id: $0) },
                          answers: planned.map { Ref(kind: "planned_session", id: $0, part: part) })
        self.init(id: id, payload: EffortPayload(time: RecordTime(start: performedAt),
                                                 status: .completed,
                                                 links: session == nil && planned == nil ? nil : links,
                                                 activityId: activityId, round: round, locationId: locationId,
                                                 metrics: metrics), prev: ["pulled-\(id)"])
    }
}
