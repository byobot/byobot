// What the app keeps on the phone, and the events it writes when you train.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Everything the app keeps on the phone. The laptop's store is the source of truth; the phone keeps
/// the plan it last pulled and every event it wrote, and pushes the ones the laptop doesn't have yet.
public struct AppState: Codable, Equatable, Sendable {
    /// This install's device id. Every event the app writes carries it.
    public var deviceId: String
    public var server: PairedServer?
    public var plan: PlanWindow?
    public var planFetchedAt: String?
    /// Every event written on this phone, in the order it was written.
    public var events: [Event]
    /// Ids of events the laptop hasn't accepted yet, oldest first.
    public var pending: [String]
    /// Events the laptop refused. Retrying won't change that, so they are kept here to show.
    public var rejected: [RejectedEvent]
    public var activeSessionId: String?
    public var lastSyncAt: String?
    public var lastHealthSyncAt: String?
    /// Sets logged elsewhere, as they stood before this phone first corrected them.
    public var remoteOriginals: [String: PerformedEffort]?
    /// Your order and swaps for each planned session, by its id.
    public var workouts: [String: WorkoutChanges]?
    /// Equipment you have, for ranking swaps. Nil means everything.
    public var equipment: [String]?
    /// The location you last trained at, for the next session you start.
    public var locationId: String?

    public init(deviceId: String = UUID7.make()) {
        self.deviceId = deviceId
        self.events = []
        self.pending = []
        self.rejected = []
    }
}

public struct RejectedEvent: Codable, Equatable, Hashable, Sendable, Identifiable {
    public var eventId: String
    public var errors: [JSONValue]
    public var at: String
    public var id: String { eventId }

    public init(eventId: String, errors: [JSONValue], at: String) {
        self.eventId = eventId
        self.errors = errors
        self.at = at
    }
}

/// Writes events for what you do, and answers what has been logged. A value type over `AppState`,
/// so it is easy to test and the app can save the state after each change.
public struct Logbook: Sendable {
    public var state: AppState
    public var actor: Actor
    public var clock: @Sendable () -> Date

    public init(state: AppState, actor: Actor = Actor(kind: .human, id: "me"),
                clock: @escaping @Sendable () -> Date = { Date() }) {
        self.state = state
        self.actor = actor
        self.clock = clock
    }

    // MARK: Writing

    @discardableResult
    mutating func append(_ payload: EventPayload) -> Event {
        let event = Event(id: UUID7.make(at: clock()), ts: Timestamp.string(clock()), deviceId: state.deviceId,
                          actor: actor, payload: payload)
        state.events.append(event)
        state.pending.append(event.id)
        return event
    }

    /// Start a session, from a planned one or on its own, where it was planned or else where you last trained.
    @discardableResult
    public mutating func startSession(planned: PlannedSession? = nil, name: String? = nil) -> PerformedSession {
        let location = planned?.locationId ?? state.locationId
        state.locationId = location
        let session = PerformedSession(id: UUID7.make(at: clock()), plannedSessionId: planned?.id, planId: planned?.planId,
                                       name: planned?.name ?? name, startedAt: Timestamp.string(clock()),
                                       locationId: location)
        append(.sessionPerformed(session))
        state.activeSessionId = session.id
        return session
    }

    /// Log one effort: a set, an interval, a stretch hold. Ties it to the active session unless told otherwise.
    @discardableResult
    public mutating func logEffort(activityId: String, metrics: Metrics, plannedEffortId: String? = nil,
                                   round: Int? = nil, side: Side? = nil, notes: String? = nil,
                                   sessionId: String? = nil) -> PerformedEffort {
        let effort = PerformedEffort(id: UUID7.make(at: clock()), activityId: activityId,
                                     performedAt: Timestamp.string(clock()), plannedEffortId: plannedEffortId,
                                     round: round, side: side, metrics: metrics.isEmpty ? nil : metrics,
                                     notes: notes?.isEmpty == true ? nil : notes,
                                     sessionId: sessionId ?? state.activeSessionId)
        append(.effortPerformed(effort))
        return effort
    }

    /// Finish the active session: log it again with its end time and what you changed from the plan.
    @discardableResult
    public mutating func finishSession(notes: String? = nil) -> PerformedSession? {
        guard var session = activeSession else { return nil }
        session.endedAt = Timestamp.string(clock())
        let changed = session.plannedSessionId.flatMap { id in state.plan?.sessions.first { $0.id == id } }
            .flatMap { workout($0).changeNotes }
        let parts = [notes?.isEmpty == false ? notes : nil, changed].compactMap { $0 }
        if !parts.isEmpty { session.notes = parts.joined(separator: " ") }
        append(.sessionPerformed(session))
        state.activeSessionId = nil
        return session
    }

    /// Correct a logged set from any device. Same id, so the newest version wins; keeps its time and round.
    @discardableResult
    public mutating func editEffort(_ original: PerformedEffort, metrics: Metrics, side: Side?, notes: String?,
                                    activityId: String? = nil, performedAt: String? = nil) -> PerformedEffort {
        if !wroteEffort(original.id) {
            var originals = state.remoteOriginals ?? [:]
            originals[original.id] = original
            state.remoteOriginals = originals
        }
        var effort = original
        effort.metrics = metrics.isEmpty ? nil : metrics
        effort.side = side
        effort.notes = notes?.isEmpty == true ? nil : notes
        if let activityId, !activityId.isEmpty { effort.activityId = activityId }
        if let performedAt { effort.performedAt = performedAt }
        append(.effortPerformed(effort))
        return effort
    }

    /// Take back everything this phone wrote for a set: one logged here goes away, one corrected here
    /// goes back to how it was logged elsewhere.
    @discardableResult
    public mutating func undoEffort(id effortId: String) -> [Event] {
        let undone = undoneEventIds
        let live = state.events.filter {
            if case .effortPerformed(let e) = $0.payload { return e.id == effortId && !undone.contains($0.id) }
            return false
        }
        var out: [Event] = []
        for event in live.reversed() {
            out.append(append(.eventUndone(Undo(eventId: event.id))))
        }
        return out
    }

    // MARK: Reading

    /// Events this phone has undone.
    public var undoneEventIds: Set<String> {
        Set(state.events.compactMap { if case .eventUndone(let u) = $0.payload { return u.eventId } else { return nil } })
    }

    public var activeSession: PerformedSession? {
        guard let id = state.activeSessionId else { return nil }
        return latestSessions[id]
    }

    /// The latest version of each session logged on this phone.
    var latestSessions: [String: PerformedSession] {
        var out: [String: (String, PerformedSession)] = [:]
        let undone = undoneEventIds
        for event in state.events where !undone.contains(event.id) {
            if case .sessionPerformed(let s) = event.payload, (out[s.id]?.0 ?? "") <= event.ts {
                out[s.id] = (event.ts, s)
            }
        }
        return out.mapValues(\.1)
    }

    /// Sessions logged on this phone, newest first.
    public var sessions: [PerformedSession] {
        latestSessions.values.sorted { $0.startedAt > $1.startedAt }
    }

    /// The latest version of each set this phone logged or corrected, in the order first logged.
    public var localEfforts: [PerformedEffort] {
        let undone = undoneEventIds
        var order: [String] = []
        var latest: [String: PerformedEffort] = [:]
        for event in state.events where !undone.contains(event.id) {
            guard case .effortPerformed(let e) = event.payload else { continue }
            if latest[e.id] == nil { order.append(e.id) }
            latest[e.id] = e
        }
        return order.compactMap { latest[$0] }
    }

    func wroteEffort(_ id: String) -> Bool {
        state.events.contains { if case .effortPerformed(let e) = $0.payload { return e.id == id } else { return false } }
    }

    /// This phone has a live version of the set, so undo can take it back.
    public func canUndo(_ id: String) -> Bool {
        localEfforts.contains { $0.id == id }
    }

    /// The set was first logged on another device.
    public func loggedElsewhere(_ id: String) -> Bool {
        state.remoteOriginals?[id] != nil
    }

    /// Sets this phone changed since the last pull, in the order first changed.
    var freshEffortIds: [String] {
        let fetched = state.planFetchedAt.flatMap(Timestamp.date)
        let pending = Set(state.pending)
        var effortOf: [String: String] = [:]
        var out: [String] = []
        for event in state.events {
            let fresh = pending.contains(event.id) || fetched.map { (Timestamp.date(event.ts) ?? .distantPast) > $0 } ?? true
            switch event.payload {
            case .effortPerformed(let e):
                effortOf[event.id] = e.id
                if fresh, !out.contains(e.id) { out.append(e.id) }
            case .eventUndone(let u):
                if fresh, let id = effortOf[u.eventId], !out.contains(id) { out.append(id) }
            default:
                break
            }
        }
        return out
    }

    public func efforts(inSession id: String) -> [PerformedEffort] {
        localEfforts.filter { $0.sessionId == id }
    }

    /// What has been logged against each planned effort and round, here and (from the last pull) anywhere
    /// else. What this phone changed since that pull wins; otherwise the pulled version does.
    public func logged(for planned: PlannedSession) -> [String: [Int: PerformedEffort]] {
        let plannedIds = Set(planned.blocks.flatMap { $0.efforts.map(\.id) })
        var current: [String: PerformedEffort] = [:]
        var order: [String] = []
        for effort in state.plan?.efforts ?? [] {
            if current[effort.id] == nil { order.append(effort.id) }
            current[effort.id] = effort
        }
        let local = Dictionary(localEfforts.map { ($0.id, $0) }, uniquingKeysWith: { _, b in b })
        for id in freshEffortIds {
            if current[id] == nil { order.append(id) }
            current[id] = local[id] ?? state.remoteOriginals?[id]
        }
        var out: [String: [Int: PerformedEffort]] = [:]
        for id in order {
            guard let effort = current[id], let pid = effort.plannedEffortId, plannedIds.contains(pid) else { continue }
            out[pid, default: [:]][effort.round ?? 1] = effort
        }
        return out
    }

    // MARK: Sync

    /// Events waiting for the laptop, oldest first.
    public var pendingEvents: [Event] {
        let byId = Dictionary(state.events.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        return state.pending.compactMap { byId[$0] }
    }

    /// Record the laptop's answer to a push of `sent`. Accepted and duplicate events are done;
    /// rejected ones are set aside with the reasons, since sending them again won't help.
    public mutating func applyPush(_ response: EventsResponse, sent: [Event]) {
        var done = Set<String>()
        for result in response.results where result.index >= 0 && result.index < sent.count {
            let id = sent[result.index].id
            switch result.status {
            case .accepted, .duplicate:
                done.insert(id)
            case .rejected:
                done.insert(id)
                state.rejected.append(RejectedEvent(eventId: id, errors: result.errors ?? [], at: Timestamp.string(clock())))
            }
        }
        state.pending.removeAll { done.contains($0) }
    }

    public mutating func applyPlan(_ window: PlanWindow) {
        state.plan = window
        state.planFetchedAt = Timestamp.string(clock())
    }
}

extension PlanWindow {
    public func activity(_ id: String) -> Activity? {
        activities.first { $0.id == id }
    }

    /// Planned sessions grouped by day, in date order.
    public var days: [(date: String, sessions: [PlannedSession])] {
        let grouped = Dictionary(grouping: sessions, by: \.scheduledOn)
        return grouped.keys.sorted().map { ($0, grouped[$0] ?? []) }
    }
}

extension PlannedSession {
    /// How many planned efforts there are, counting each round.
    public var plannedCount: Int {
        blocks.reduce(0) { $0 + ($1.rounds ?? 1) * $1.efforts.count }
    }
}
