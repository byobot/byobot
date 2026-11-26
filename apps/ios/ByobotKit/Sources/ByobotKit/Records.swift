// Records as the laptop sends them, and what the app needs to write events about them: hybrid
// logical clocks, derived ids and the payloads of the kinds a person writes.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// A live record: its id, its current payload, and the events a change to it names as `prev`.
@dynamicMemberLookup
public struct Record<Payload: Codable & Hashable & Sendable>: Codable, Equatable, Hashable, Sendable, Identifiable {
    public var id: String
    public var payload: Payload
    public var prev: [String]
    /// Two writers changed it at once; this is the version reads show.
    public var conflict: Bool?
    /// An activity that ships with byobot, not yet written to the logbook.
    public var library: Bool?
    /// The kind version its payload needs (1 when absent). Above this app's, the app leaves the record as it is.
    public var kindVersion: Int?

    public init(id: String, payload: Payload, prev: [String] = [], conflict: Bool? = nil, library: Bool? = nil,
                kindVersion: Int? = nil) {
        self.id = id
        self.payload = payload
        self.prev = prev
        self.conflict = conflict
        self.library = library
        self.kindVersion = kindVersion
    }

    enum CodingKeys: String, CodingKey {
        case id, payload, prev, conflict, library
        case kindVersion = "kind_version"
    }

    public subscript<T>(dynamicMember path: KeyPath<Payload, T>) -> T {
        payload[keyPath: path]
    }

    public subscript<T>(dynamicMember path: WritableKeyPath<Payload, T>) -> T {
        get { payload[keyPath: path] }
        set { payload[keyPath: path] = newValue }
    }
}

extension KindVersion {
    /// The lowest version of `kind` that holds `payload`. Writes are stamped with it, so installs that know only
    /// older versions still read them.
    public static func lowest(_ kind: String, _ payload: JSONValue) -> Int {
        var version = reshaped[kind] ?? 1
        if case .object(let fields) = payload, let added = since[kind] {
            for key in fields.keys { version = max(version, added[key] ?? 1) }
        }
        return min(version, all[kind] ?? 1)
    }
}

public typealias Activity = Record<ActivityPayload>
public typealias Location = Record<LocationPayload>
public typealias Plan = Record<PlanPayload>
public typealias PlannedSession = Record<PlannedSessionPayload>
public typealias PerformedSession = Record<SessionPayload>
public typealias PerformedEffort = Record<EffortPayload>
public typealias Overlay = Record<OverlayPayload>
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
    /// Rearrangements of the planned sessions in the window.
    public var overlays: [Overlay]
    /// Sessions logged against the planned sessions.
    public var sessions: [PerformedSession]
    /// Efforts logged against them, and the last two times each planned activity was done at each location.
    public var efforts: [PerformedEffort]

    public init(from: String, to: String, activities: [Activity] = [], locations: [Location] = [],
                settings: [Setting] = [], plans: [Plan] = [], plannedSessions: [PlannedSession] = [],
                overlays: [Overlay] = [], sessions: [PerformedSession] = [], efforts: [PerformedEffort] = []) {
        self.from = from
        self.to = to
        self.activities = activities
        self.locations = locations
        self.settings = settings
        self.plans = plans
        self.plannedSessions = plannedSessions
        self.overlays = overlays
        self.sessions = sessions
        self.efforts = efforts
    }

    enum CodingKeys: String, CodingKey {
        case from, to, activities, locations, settings, plans, overlays, sessions, efforts
        case plannedSessions = "planned_sessions"
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
        (try? JSONValue.parse(JSONEncoder().encode(value))) ?? .object([:])
    }

    static func decode<T: Decodable>(_ type: T.Type, _ value: JSONValue) -> T? {
        guard let data = try? JSONEncoder().encode(value) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }
}

extension Event {
    /// The payload as a kind's type, when it is one.
    public func decoded<T: Decodable>(_ type: T.Type) -> T? {
        op == .put ? Payloads.decode(T.self, payload) : nil
    }
}

/// Hybrid logical clock readings: Unix milliseconds and a counter, compared as text.
public enum HLC {
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
        var bytes = Array(SHA1.digest(ns + name).prefix(16))
        bytes[6] = (bytes[6] & 0x0F) | 0x50
        bytes[8] = (bytes[8] & 0x3F) | 0x80
        let h = bytes.map { String(format: "%02x", $0) }.joined()
        return [h.prefix(8), h.dropFirst(8).prefix(4), h.dropFirst(12).prefix(4), h.dropFirst(16).prefix(4),
                h.dropFirst(20)].joined(separator: "-")
    }
}

/// SHA-1, for UUIDv5 only.
enum SHA1 {
    static func digest(_ message: [UInt8]) -> [UInt8] {
        var h: [UInt32] = [0x6745_2301, 0xEFCD_AB89, 0x98BA_DCFE, 0x1032_5476, 0xC3D2_E1F0]
        var m = message
        let bits = UInt64(message.count) * 8
        m.append(0x80)
        while m.count % 64 != 56 { m.append(0) }
        for i in (0..<8).reversed() { m.append(UInt8((bits >> UInt64(8 * i)) & 0xFF)) }
        for chunk in stride(from: 0, to: m.count, by: 64) {
            var w = [UInt32](repeating: 0, count: 80)
            for t in 0..<16 {
                let b = chunk + 4 * t
                w[t] = UInt32(m[b]) << 24 | UInt32(m[b + 1]) << 16 | UInt32(m[b + 2]) << 8 | UInt32(m[b + 3])
            }
            for t in 16..<80 { w[t] = rotl(w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16], 1) }
            var (a, b, c, d, e) = (h[0], h[1], h[2], h[3], h[4])
            for t in 0..<80 {
                let (f, k): (UInt32, UInt32)
                switch t {
                case 0..<20: (f, k) = ((b & c) | (~b & d), 0x5A82_7999)
                case 20..<40: (f, k) = (b ^ c ^ d, 0x6ED9_EBA1)
                case 40..<60: (f, k) = ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC)
                default: (f, k) = (b ^ c ^ d, 0xCA62_C1D6)
                }
                let temp = rotl(a, 5) &+ f &+ e &+ k &+ w[t]
                (e, d, c, b, a) = (d, c, rotl(b, 30), a, temp)
            }
            h[0] = h[0] &+ a
            h[1] = h[1] &+ b
            h[2] = h[2] &+ c
            h[3] = h[3] &+ d
            h[4] = h[4] &+ e
        }
        return h.flatMap { v in (0..<4).map { UInt8((v >> UInt32(24 - 8 * $0)) & 0xFF) } }
    }

    static func rotl(_ x: UInt32, _ n: UInt32) -> UInt32 { (x << n) | (x >> (32 - n)) }
}
