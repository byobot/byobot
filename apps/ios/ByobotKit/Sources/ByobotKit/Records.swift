// Records as the laptop sends them, and what the app needs to write events about them: hybrid
// logical clocks, derived ids and the payloads of the kinds a person writes.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import CryptoKit
import Foundation

/// A live record: its id, its current payload, and the events a change to it names as `prev`.
@dynamicMemberLookup
public struct Record<Payload: Codable & Hashable & Sendable>: Codable, Equatable, Hashable, Sendable, Identifiable {
    public var id: String
    public var payload: Payload { didSet { raw = nil } }
    public var prev: [String]
    /// Two writers changed it at once; this is the version reads show.
    public var conflict: Bool?
    /// An activity that ships with byobot, not yet written to the logbook.
    public var library: Bool?
    /// The kind version its payload needs (1 when absent). Above this app's, the app leaves the record as it is.
    public var kindVersion: Int?
    /// In a conflict, every current version, the one shown included.
    public var versions: [ConflictVersion]?
    /// The payload as written, fields this app doesn't know included, until `payload` changes here.
    public internal(set) var raw: JSONValue?

    public init(id: String, payload: Payload, prev: [String] = [], conflict: Bool? = nil, library: Bool? = nil,
                kindVersion: Int? = nil, versions: [ConflictVersion]? = nil, raw: JSONValue? = nil) {
        self.id = id
        self.payload = payload
        self.prev = prev
        self.conflict = conflict
        self.library = library
        self.kindVersion = kindVersion
        self.versions = versions
        self.raw = raw
    }

    enum CodingKeys: String, CodingKey {
        case id, payload, prev, conflict, library, versions
        case kindVersion = "kind_version"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        payload = try c.decode(Payload.self, forKey: .payload)
        prev = try c.decodeIfPresent([String].self, forKey: .prev) ?? []
        conflict = try c.decodeIfPresent(Bool.self, forKey: .conflict)
        library = try c.decodeIfPresent(Bool.self, forKey: .library)
        kindVersion = try c.decodeIfPresent(Int.self, forKey: .kindVersion)
        versions = try c.decodeIfPresent([ConflictVersion].self, forKey: .versions)
        raw = try c.decode(JSONValue.self, forKey: .payload)
    }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        if let raw { try c.encode(raw, forKey: .payload) } else { try c.encode(payload, forKey: .payload) }
        try c.encode(prev, forKey: .prev)
        try c.encodeIfPresent(conflict, forKey: .conflict)
        try c.encodeIfPresent(library, forKey: .library)
        try c.encodeIfPresent(kindVersion, forKey: .kindVersion)
        try c.encodeIfPresent(versions, forKey: .versions)
    }

    public static func == (a: Record, b: Record) -> Bool {
        a.id == b.id && a.payload == b.payload && a.prev == b.prev && a.conflict == b.conflict
            && a.library == b.library && a.kindVersion == b.kindVersion && a.versions == b.versions
    }

    public func hash(into hasher: inout Hasher) {
        hasher.combine(id)
        hasher.combine(payload)
        hasher.combine(prev)
    }

    public subscript<T>(dynamicMember path: KeyPath<Payload, T>) -> T {
        payload[keyPath: path]
    }

    public subscript<T>(dynamicMember path: WritableKeyPath<Payload, T>) -> T {
        get { payload[keyPath: path] }
        set { payload[keyPath: path] = newValue }
    }
}

public typealias Activity = Record<ActivityPayload>
public typealias Location = Record<LocationPayload>
public typealias Plan = Record<PlanPayload>
public typealias PlannedSession = Record<PlannedSessionPayload>
public typealias PerformedSession = Record<SessionPayload>
public typealias PerformedEffort = Record<EffortPayload>
public typealias Setting = Record<SettingPayload>

/// GET /v1/plan: everything the app needs to run the sessions scheduled in the window.
public struct PlanWindow: Codable, Equatable, Sendable {
    public var from: String
    public var to: String
    public var activities: [Activity]
    public var locations: [Location]
    public var settings: [Setting]
    public var plans: [Plan]
    public var plannedSessions: [PlannedSession]
    /// Sessions logged against the planned sessions, those from `since` on, and those holding the last two times
    /// each activity was done at each location.
    public var sessions: [PerformedSession]
    /// The efforts of those sessions and of the planned ones, and those outside a session from `since` on.
    public var efforts: [PerformedEffort]
    // Older laptops send none of these.
    /// The first day of logged history the plan carries.
    public var since: String?
    /// The laptop's store as of this read; it changes when the laptop is restored from another store's backup.
    public var storeId: String?
    /// The laptop's clock.
    public var hlc: String?
    /// Locations and settings removed, not erased: a new one takes another id, a change names their `prev`.
    public var removed: [RemovedRecord]?
    /// Records a person writes that were erased, so the phone drops what it holds of them.
    public var erased: [ErasedRecord]?
    public var kindVersions: [String: Int]?
    /// The latest of this phone's events the laptop holds; empty when it holds none.
    public var received: [String: String]?
    /// This phone's events the laptop refused.
    public var rejected: [Rejection]?
    /// Entries this app can't read, by the list they came in, kept as sent: one newer entry doesn't drop the plan.
    public var unread: [String: [JSONValue]] = [:]

    public init(from: String, to: String, activities: [Activity] = [], locations: [Location] = [],
                settings: [Setting] = [], plans: [Plan] = [], plannedSessions: [PlannedSession] = [],
                sessions: [PerformedSession] = [], efforts: [PerformedEffort] = [], since: String? = nil,
                storeId: String? = nil, hlc: String? = nil, removed: [RemovedRecord]? = nil, erased: [ErasedRecord]? = nil,
                kindVersions: [String: Int]? = nil, received: [String: String]? = nil, rejected: [Rejection]? = nil) {
        self.from = from
        self.to = to
        self.activities = activities
        self.locations = locations
        self.settings = settings
        self.plans = plans
        self.plannedSessions = plannedSessions
        self.sessions = sessions
        self.efforts = efforts
        self.since = since
        self.storeId = storeId
        self.hlc = hlc
        self.removed = removed
        self.erased = erased
        self.kindVersions = kindVersions
        self.received = received
        self.rejected = rejected
    }

    enum CodingKeys: String, CodingKey {
        case from, to, since, activities, locations, settings, plans, sessions, efforts, hlc, removed, erased, received
        case rejected
        case plannedSessions = "planned_sessions"
        case kindVersions = "kind_versions"
        case storeId = "store_id"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var unread: [String: [JSONValue]] = [:]
        func entries<P: Codable & Hashable & Sendable>(_ key: CodingKeys) throws -> [Record<P>] {
            let items = try c.decode([Lenient<Record<P>>].self, forKey: key)
            let skipped = items.compactMap(\.raw)
            if !skipped.isEmpty { unread[key.stringValue] = skipped }
            return items.compactMap(\.value)
        }
        from = try c.decode(String.self, forKey: .from)
        to = try c.decode(String.self, forKey: .to)
        activities = try entries(.activities)
        locations = try entries(.locations)
        settings = try entries(.settings)
        plans = try entries(.plans)
        plannedSessions = try entries(.plannedSessions)
        sessions = try entries(.sessions)
        efforts = try entries(.efforts)
        since = try c.decodeIfPresent(String.self, forKey: .since)
        storeId = try c.decodeIfPresent(String.self, forKey: .storeId)
        hlc = try c.decodeIfPresent(String.self, forKey: .hlc)
        removed = try c.decodeIfPresent([RemovedRecord].self, forKey: .removed)
        erased = try c.decodeIfPresent([ErasedRecord].self, forKey: .erased)
        kindVersions = try c.decodeIfPresent([String: Int].self, forKey: .kindVersions)
        received = try c.decodeIfPresent([String: String].self, forKey: .received)
        rejected = try c.decodeIfPresent([Rejection].self, forKey: .rejected)
        self.unread = unread
    }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        func entries<P: Codable & Hashable & Sendable>(_ records: [Record<P>], _ key: CodingKeys) throws {
            guard let kept = unread[key.stringValue], !kept.isEmpty else { return try c.encode(records, forKey: key) }
            try c.encode(records.map { Payloads.json($0) } + kept, forKey: key)
        }
        try c.encode(from, forKey: .from)
        try c.encode(to, forKey: .to)
        try entries(activities, .activities)
        try entries(locations, .locations)
        try entries(settings, .settings)
        try entries(plans, .plans)
        try entries(plannedSessions, .plannedSessions)
        try entries(sessions, .sessions)
        try entries(efforts, .efforts)
        try c.encodeIfPresent(since, forKey: .since)
        try c.encodeIfPresent(storeId, forKey: .storeId)
        try c.encodeIfPresent(hlc, forKey: .hlc)
        try c.encodeIfPresent(removed, forKey: .removed)
        try c.encodeIfPresent(erased, forKey: .erased)
        try c.encodeIfPresent(kindVersions, forKey: .kindVersions)
        try c.encodeIfPresent(received, forKey: .received)
        try c.encodeIfPresent(rejected, forKey: .rejected)
    }
}

/// One item of a list read leniently: the value, or what was sent when this app can't read it.
struct Lenient<T: Decodable>: Decodable {
    var value: T?
    var raw: JSONValue?

    init(from decoder: any Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let value = try? c.decode(T.self) {
            self.value = value
        } else {
            raw = try c.decode(JSONValue.self)
        }
    }
}

extension Record where Payload == EffortPayload {
    public var performedAt: String { payload.time.start }
    public var sessionId: String? { payload.links?.partOf?.id }
    public var plannedSessionId: String? { payload.links?.answers?.id }
    public var plannedEffortId: String? { payload.links?.answers?.part }
}

extension Record where Payload == SessionPayload {
    public var startedAt: String { payload.time.start }
    public var endedAt: String? { payload.time.end }
    public var plannedSessionId: String? { payload.links?.answers?.id }
    public var planId: String? { payload.links?.partOf?.id }
}

extension Record where Payload == PlannedSessionPayload {
    public var planId: String? { payload.links?.partOf?.id }
}

/// A payload as JSON, and back.
enum Payloads {
    static func json<T: Encodable>(_ value: T) -> JSONValue {
        // Every payload type here encodes; a failure would be a bug in the generated types.
        (try? encode(value)) ?? .object([:])
    }

    static func encode<T: Encodable>(_ value: T) throws -> JSONValue {
        try JSONValue.parse(JSONEncoder().encode(value))
    }

    static func decode<T: Decodable>(_ type: T.Type, _ value: JSONValue) throws -> T {
        try JSONDecoder().decode(type, from: JSONEncoder().encode(value))
    }

    /// `raw` with only what changed from `old` to `new`: fields this app doesn't know, and values it didn't
    /// touch, stay exactly as written.
    static func patch(_ raw: JSONValue, from old: JSONValue, to new: JSONValue) -> JSONValue {
        guard case .object(var out) = raw, case .object(let before) = old, case .object(let after) = new else {
            return old == new ? raw : new
        }
        for key in Set(before.keys).union(after.keys) where before[key] != after[key] {
            guard let value = after[key] else {
                out[key] = nil
                continue
            }
            if let was = before[key], let kept = out[key] {
                out[key] = patch(kept, from: was, to: value)
            } else {
                out[key] = value
            }
        }
        return .object(out)
    }
}

extension Event {
    /// The payload as a kind's type, when it is one.
    public func decoded<T: Decodable>(_ type: T.Type) -> T? {
        op == .put ? (try? Payloads.decode(T.self, payload)) : nil
    }
}

/// Hybrid logical clock readings: Unix milliseconds and a counter, compared as text.
public enum HLC {
    /// Readings at or past 2200 are refused, as the laptop refuses them.
    public static let ceiling = format(ms: 7_258_118_400_000, counter: 0)

    public static func format(ms: UInt64, counter: Int) -> String {
        String(format: "%013llu-%06ld", ms, counter)
    }

    public static func parse(_ text: String) -> (ms: UInt64, counter: Int)? {
        let parts = text.split(separator: "-")
        guard parts.count == 2, let ms = UInt64(parts[0]), let counter = Int(parts[1]) else { return nil }
        return (ms, counter)
    }

    /// The next reading after `last`: the wall clock if it has moved on, else one more on the counter.
    public static func next(after last: String?, now: Date) -> String {
        let wall = UInt64(Swift.max(0, now.timeIntervalSince1970) * 1000)
        guard let last = last.flatMap(parse), last.ms >= wall else { return format(ms: wall, counter: 0) }
        return last.counter >= 999_999 ? format(ms: last.ms + 1, counter: 0) : format(ms: last.ms, counter: last.counter + 1)
    }
}

/// Ids derived from other ids: UUIDv5 over the RFC 8785 JSON of their parts.
public enum DerivedId {
    public static func make(_ namespace: String, _ parts: [String]) -> String {
        uuid5(namespace: namespace, name: Array(canonical(parts).utf8))
    }

    /// The overlay that holds your intent about a record, one per effect and target.
    public static func overlay(_ effect: OverlayEffect, kind: String, id: String) -> String {
        make(Namespace.overlay, [effect.rawValue, kind, id])
    }

    /// A JSON array of strings with nothing between the tokens, as RFC 8785 writes it.
    static func canonical(_ parts: [String]) -> String {
        "[" + parts.map(quote).joined(separator: ",") + "]"
    }

    static func quote(_ s: String) -> String {
        var out = "\""
        for scalar in s.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\u{08}": out += "\\b"
            case "\u{0C}": out += "\\f"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case _ where scalar.value < 0x20: out += String(format: "\\u%04x", scalar.value)
            default: out.unicodeScalars.append(scalar)
            }
        }
        return out + "\""
    }

    static func uuid5(namespace: String, name: [UInt8]) -> String {
        let hex = namespace.replacingOccurrences(of: "-", with: "")
        var ns: [UInt8] = []
        var i = hex.startIndex
        while i < hex.endIndex {
            let j = hex.index(i, offsetBy: 2)
            ns.append(UInt8(hex[i..<j], radix: 16) ?? 0)
            i = j
        }
        var bytes = Array(Insecure.SHA1.hash(data: Data(ns + name)).prefix(16))
        bytes[6] = (bytes[6] & 0x0F) | 0x50
        bytes[8] = (bytes[8] & 0x3F) | 0x80
        return bytes.uuid
    }
}
