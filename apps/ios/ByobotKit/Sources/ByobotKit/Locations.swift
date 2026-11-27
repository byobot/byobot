// Gyms and other places you train. Loads compare within one, because the same plates and machines
// can feel different at another gym.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// The sets you did last time, and whether their loads were at another gym than today's.
public struct LastTime: Equatable, Sendable {
    public var efforts: [PerformedEffort]
    public var locationId: String?
    /// Done somewhere other than where you are now (or somewhere unrecorded), so the load may not carry over.
    public var elsewhere: Bool

    public static let empty = LastTime(efforts: [], locationId: nil, elsewhere: false)
}

public enum Slug {
    /// An id from what someone typed: lowercase letters, digits and underscores. `Pull Up` → `pull_up`.
    public static func make(_ text: String, max: Int = 64) -> String {
        let words = text.lowercased().split { !($0.isASCII && ($0.isLetter || $0.isNumber)) }
        return String(words.joined(separator: "_").prefix(max))
    }
}

public enum LocationId {
    /// A slug for a new location's name, unique among `taken`.
    public static func make(_ name: String, taken: Set<String>) -> String {
        var base = Slug.make(name, max: 60)
        if base.isEmpty { base = "gym" }
        var id = base
        var n = 2
        while taken.contains(id) {
            id = "\(base)_\(n)"
            n += 1
        }
        return id
    }
}

extension Logbook {
    /// Locations from your laptop and any added on this phone, by name.
    public var locations: [Location] {
        var byId = Dictionary((state.plan?.locations ?? []).map { ($0.id, $0) }, uniquingKeysWith: { _, b in b })
        for location in written("location", as: LocationPayload.self) { byId[location.id] = location }
        return byId.values.sorted { ($0.name.lowercased(), $0.id) < ($1.name.lowercased(), $1.id) }
    }

    public func location(_ id: String?) -> Location? {
        id.flatMap { id in locations.first { $0.id == id } }
    }

    /// Where you are: the active session's location, else the last one you picked.
    public var currentLocationId: String? {
        if let session = activeSession { return session.locationId }
        return state.locationId
    }

    /// Add a location by name and train there from now on.
    @discardableResult
    public mutating func addLocation(name: String) -> Location {
        let erased = (state.plan?.erased ?? []).filter { $0.kind == "location" }.map(\.id)
        let id = LocationId.make(name, taken: Set(locations.map(\.id) + erased))  // an erased id can't be written again
        let payload = LocationPayload(name: name.trimmingCharacters(in: .whitespacesAndNewlines))
        let event = put("location", id, payload)
        setLocation(id)
        return Location(id: id, payload: payload, prev: [event.id])
    }

    /// Where you're training. During a session it moves the whole session, sets already logged included.
    public mutating func setLocation(_ id: String?) {
        if id != state.locationId { setCurrentLocation(id) }
        guard var session = activeSession, session.locationId != id else { return }
        session.payload.locationId = id
        put("session", session.id, session.payload)
    }

    /// Where a set happened: its own location, else its session's.
    public func locationId(of effort: PerformedEffort) -> String? {
        locationId(of: effort, sessions: latestSessions)
    }

    func locationId(of effort: PerformedEffort, sessions: [String: PerformedSession]) -> String? {
        if let own = effort.locationId { return own }
        guard let session = effort.sessionId else { return nil }
        return sessions[session]?.locationId ?? state.plan?.sessions.first { $0.id == session }?.locationId
    }

    /// The sets of an activity from the last time you did it outside `sessionId`, in the order done.
    /// Sets with a load compare only at `locationId`; with no history there, the latest anywhere comes back
    /// marked as elsewhere.
    public func lastTime(_ activityId: String, excludingSession sessionId: String? = nil,
                         at place: String?) -> LastTime {
        let mine = efforts(of: activityId).filter { $0.sessionId == nil || $0.sessionId != sessionId }
        guard let here = place, mine.contains(where: { $0.metrics?["load"] != nil }) else {
            return LastTime(efforts: Self.latestOccasion(mine), locationId: nil, elsewhere: false)
        }
        let sessions = latestSessions
        let atHere = mine.filter { self.locationId(of: $0, sessions: sessions) == here }
        if !atHere.isEmpty {
            return LastTime(efforts: Self.latestOccasion(atHere), locationId: here, elsewhere: false)
        }
        let latest = Self.latestOccasion(mine)
        return LastTime(efforts: latest, locationId: latest.first.flatMap { self.locationId(of: $0, sessions: sessions) },
                        elsewhere: !latest.isEmpty)
    }

    static func latestOccasion(_ efforts: [PerformedEffort]) -> [PerformedEffort] {
        let date = { (e: PerformedEffort) in Timestamp.date(e.performedAt) ?? .distantPast }
        guard let latest = efforts.max(by: { date($0) < date($1) }) else { return [] }
        guard let session = latest.sessionId else { return [latest] }
        return efforts.filter { $0.sessionId == session }.sorted { date($0) < date($1) }
    }

    /// The planned session's loads were set for another location than the one you're at.
    public func plannedElsewhere(_ planned: PlannedSession) -> Bool {
        guard let plannedAt = planned.locationId, let here = currentLocationId else { return false }
        return plannedAt != here
    }
}
