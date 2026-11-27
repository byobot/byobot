// Records two writers changed at once: what each wrote, and the write that ends it.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// A record in conflict as the last plan showed it, with each of its current versions.
public struct Conflict: Identifiable, Equatable, Sendable {
    public var kind: String
    public var recordId: String
    /// What reads show meanwhile.
    public var shown: JSONValue
    public var versions: [ConflictVersion]

    public var id: String { "\(kind)/\(recordId)" }

    /// The record's name, where it has one.
    public var name: String? {
        if case .object(let fields) = shown, case .string(let name)? = fields["name"] { return name }
        return nil
    }

    /// The fields the versions don't agree on, by name.
    public var fields: [String] {
        let payloads = versions.map { (version: ConflictVersion) -> [String: JSONValue] in
            if case .object(let fields)? = version.payload { return fields }
            return [:]
        }
        let names = Set(payloads.flatMap(\.keys))
        return names.filter { name in Set(payloads.map { $0[name] }).count > 1 }.sorted()
    }
}

extension Logbook {
    /// Records in conflict that this phone hasn't changed since the plan showed them.
    public var conflicts: [Conflict] {
        guard let plan = state.plan else { return [] }
        let pending = Set(state.pending)
        func open<P: Codable & Hashable & Sendable>(_ kind: String, _ records: [Record<P>]) -> [Conflict] {
            records.compactMap { (record: Record<P>) -> Conflict? in
                guard record.conflict == true, let versions = record.versions, versions.count > 1 else { return nil }
                if let mine = lastEvent(kind, record.id), fresh(mine, pending: pending) { return nil }
                return Conflict(kind: kind, recordId: record.id, shown: Payloads.json(record.payload), versions: versions)
            }
        }
        return open("activity", plan.activities) + open("location", plan.locations) + open("setting", plan.settings)
            + open("plan", plan.plans) + open("planned_session", plan.plannedSessions) + open("session", plan.sessions)
            + open("effort", plan.efforts)
    }

    /// Ends a conflict with one of its versions. The write names every version as prev, which is what ends it.
    @discardableResult
    public mutating func resolve(_ conflict: Conflict, keeping version: ConflictVersion) -> Event {
        let prev = conflict.versions.map(\.eventId)
        guard let payload = version.payload else {
            return append(conflict.kind, conflict.recordId, .retract, .object([:]), prev: prev, reverts: nil)
        }
        return append(conflict.kind, conflict.recordId, .put, payload, prev: prev, reverts: nil)
    }
}
