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
    /// Ids of events the laptop hasn't taken yet, oldest first.
    public var pending: [String]
    /// Events the laptop refused. Retrying won't change that, so they are kept here to show.
    public var rejected: [RejectedEvent]
    public var activeSessionId: String?
    public var lastSyncAt: String?
    public var lastHealthSyncAt: String?
    /// The last clock reading this phone wrote.
    public var hlc: String?
    /// For each record written here (`kind/id`), the events a change to it names as `prev`, until a pull
    /// brings the laptop's.
    public var heads: [String: [String]]?
    /// Sets logged elsewhere, as they stood before this phone first corrected them.
    public var remoteOriginals: [String: PerformedEffort]?
    /// Your order and swaps for each planned session, by its id, until a pull brings the laptop's.
    public var workouts: [String: WorkoutChanges]?
    /// Equipment you have, for ranking swaps. Nil means everything.
    public var equipment: [String]?
    /// Where the next session starts.
    public var locationId: String?
    /// The store that holds what this phone sent.
    public var storeId: String?
    /// What was sent up to this clock reading went to another store, and is never sent again.
    public var resendAfter: String?
    /// What was sent up to this clock reading, before the app kept which store it went to. The first plan
    /// shows whether this store holds it.
    public var unsureAfter: String?

    public init(deviceId: String = UUID7.make()) {
        self.deviceId = deviceId
        self.events = []
        self.pending = []
        self.rejected = []
    }
}

public struct RejectedEvent: Codable, Equatable, Hashable, Sendable, Identifiable {
    public var eventId: String
    public var reason: String?
    public var at: String
    public var id: String { eventId }

    public init(eventId: String, reason: String?, at: String) {
        self.eventId = eventId
        self.reason = reason
        self.at = at
    }
}

/// This phone's part of the byobot logbook: writes events for what you do, and answers what has been
/// logged. A value type over `AppState`, so it is easy to test and the app can save the state after each change.
public struct Logbook: Sendable {
    public var state: AppState
    public var actor: Actor
    /// The app and device that capture what you log.
    public var origin: Origin?
    public var clock: @Sendable () -> Date

    public init(state: AppState, actor: Actor = Actor(kind: .human, id: "me"), origin: Origin? = nil,
                clock: @escaping @Sendable () -> Date = { Date() }) {
        self.state = state
        self.actor = actor
        self.origin = origin
        self.clock = clock
    }

    // MARK: Writing

    static func key(_ kind: String, _ id: String) -> String { "\(kind)/\(id)" }

    @discardableResult
    mutating func put<P: Encodable>(_ kind: String, _ id: String, _ payload: P, known: [String]? = nil,
                                    reverts: String? = nil) -> Event {
        append(kind, id, .put, Payloads.json(payload), known: known, reverts: reverts)
    }

    @discardableResult
    mutating func retract(_ kind: String, _ id: String, reverts: String? = nil) -> Event {
        append(kind, id, .retract, .object([:]), known: nil, reverts: reverts)
    }

    private mutating func append(_ kind: String, _ id: String, _ op: Op, _ payload: JSONValue, known: [String]?,
                                 reverts: String?) -> Event {
        let now = clock()
        let hlc = HLC.next(after: state.hlc, now: now)
        let event = Event(id: UUID7.make(at: now), schemaVersion: ContractVersion.schema,
                          record: RecordRef(kind: kind, id: id), op: op,
                          kindVersion: op == .put ? KindVersion.lowest(kind, payload) : nil, hlc: hlc, ts: Timestamp.string(now),
                          deviceId: state.deviceId, actor: actor, prev: prev(kind, id, known: known), reverts: reverts,
                          payload: payload)
        state.hlc = hlc
        state.events.append(event)
        state.pending.append(event.id)
        var heads = state.heads ?? [:]
        heads[Self.key(kind, id)] = [event.id]
        state.heads = heads
        return event
    }

    /// The events a change to the record names as `prev`: this phone's last write, else the laptop's.
    func prev(_ kind: String, _ id: String, known: [String]? = nil) -> [String] {
        if let mine = state.heads?[Self.key(kind, id)] { return mine }
        if let known { return known }
        return entry(kind, id)?.prev ?? []
    }

    /// Whether the laptop's version of the record needs a newer app. Changing it here would drop what this app
    /// can't read, so the app leaves it as it is.
    public func isNewer(_ kind: String, _ id: String) -> Bool {
        state.heads?[Self.key(kind, id)] == nil && (entry(kind, id)?.kindVersion ?? 1) > (KindVersion.all[kind] ?? 1)
    }

    private func entry(_ kind: String, _ id: String) -> (prev: [String], kindVersion: Int?)? {
        guard let plan = state.plan else { return nil }
        func find<P: Codable & Hashable & Sendable>(_ records: [Record<P>]) -> (prev: [String], kindVersion: Int?)? {
            records.first { $0.id == id }.map { ($0.prev, $0.kindVersion) }
        }
        switch kind {
        case "activity": return find(plan.activities)
        case "location": return find(plan.locations)
        case "setting": return find(plan.settings)
        case "plan": return find(plan.plans)
        case "planned_session": return find(plan.plannedSessions)
        case "overlay": return find(plan.overlays)
        case "session": return find(plan.sessions)
        case "effort": return find(plan.efforts)
        default: return nil
        }
    }

    func now() -> RecordTime {
        RecordTime(start: Timestamp.string(clock()), zone: Timestamp.zone())
    }

    /// Start a session, from a planned one or on its own, where it was planned or else where you last trained.
    @discardableResult
    public mutating func startSession(planned: PlannedSession? = nil, name: String? = nil) -> PerformedSession {
        let location = planned?.locationId ?? state.locationId
        if location != state.locationId { setCurrentLocation(location) }
        let links = planned.map { p in
            Links(partOf: p.planId.map { Ref(kind: "plan", id: $0) }, answers: Ref(kind: "planned_session", id: p.id))
        }
        let id = UUID7.make(at: clock())
        let payload = SessionPayload(time: now(), modality: .selfReported, origin: origin, status: .inProgress,
                                     links: links, name: planned?.name ?? name, locationId: location)
        let event = put("session", id, payload)
        state.activeSessionId = id
        return PerformedSession(id: id, payload: payload, prev: [event.id])
    }

    /// Log one effort: a set, an interval, a stretch hold. Ties it to the active session unless told otherwise.
    @discardableResult
    public mutating func logEffort(activityId: String, metrics: Metrics, plannedEffortId: String? = nil,
                                   round: Int? = nil, side: Side? = nil, notes: String? = nil,
                                   sessionId: String? = nil) -> PerformedEffort {
        let session = sessionId ?? state.activeSessionId
        let answers = plannedEffortId.flatMap { part in
            plannedSessionId(containing: part, session: session).map { Ref(kind: "planned_session", id: $0, part: part) }
        }
        let partOf = session.map { Ref(kind: "session", id: $0) }
        let links = partOf == nil && answers == nil ? nil : Links(partOf: partOf, answers: answers)
        let id = UUID7.make(at: clock())
        let payload = EffortPayload(time: now(), modality: .selfReported, origin: origin, status: .completed,
                                    links: links, activityId: activityId, round: round, side: side,
                                    metrics: metrics.isEmpty ? nil : metrics, notes: notes?.isEmpty == true ? nil : notes)
        let event = put("effort", id, payload)
        return PerformedEffort(id: id, payload: payload, prev: [event.id])
    }

    /// The planned session a planned effort belongs to: the active session's, else the first in the plan that has it.
    func plannedSessionId(containing part: String, session: String?) -> String? {
        let planned = state.plan?.plannedSessions ?? []
        let has = { (id: String) in
            planned.first { $0.id == id }?.blocks.contains { $0.efforts.contains { $0.id == part } } ?? false
        }
        let answered = session.flatMap { latestSessions[$0]?.plannedSessionId }
        if let answered, has(answered) { return answered }
        return planned.first { has($0.id) }?.id ?? answered
    }

    /// Finish the active session: write it again with its end time and what you changed from the plan.
    @discardableResult
    public mutating func finishSession(notes: String? = nil) -> PerformedSession? {
        guard var session = activeSession else { return nil }
        session.payload.time.end = Timestamp.string(clock())
        session.payload.status = .completed
        let changed = session.plannedSessionId.flatMap { id in state.plan?.plannedSessions.first { $0.id == id } }
            .flatMap { workout($0).changeNotes }
        let parts = [notes?.isEmpty == false ? notes : nil, changed].compactMap { $0 }
        if !parts.isEmpty { session.payload.notes = parts.joined(separator: " ") }
        let event = put("session", session.id, session.payload)
        state.activeSessionId = nil
        session.prev = [event.id]
        return session
    }

    /// Correct a logged set from any device. Keeps its round; naming its current version as prev means a
    /// change made elsewhere at the same time shows as a conflict instead of being lost.
    @discardableResult
    public mutating func editEffort(_ original: PerformedEffort, metrics: Metrics, side: Side?, notes: String?,
                                    activityId: String? = nil, performedAt: String? = nil) -> PerformedEffort {
        guard !isNewer("effort", original.id) else { return original }
        if !wrote("effort", original.id) {
            var originals = state.remoteOriginals ?? [:]
            originals[original.id] = original
            state.remoteOriginals = originals
        }
        var effort = original
        effort.payload.metrics = metrics.isEmpty ? nil : metrics
        effort.payload.side = side
        effort.payload.notes = notes?.isEmpty == true ? nil : notes
        if let activityId, !activityId.isEmpty { effort.payload.activityId = activityId }
        if let performedAt {
            effort.payload.time.start = performedAt
            if let end = effort.payload.time.end, end < performedAt { effort.payload.time.end = nil }
        }
        let event = put("effort", original.id, effort.payload, known: original.prev)
        effort.prev = [event.id]
        return effort
    }

    /// Take back what this phone wrote for a set: one logged here goes away, one corrected here goes back
    /// to how it was logged elsewhere.
    @discardableResult
    public mutating func undoEffort(id effortId: String) -> [Event] {
        guard canUndo(effortId), let last = lastEvent("effort", effortId) else { return [] }
        if let original = state.remoteOriginals?[effortId] {
            return [put("effort", effortId, original.payload, reverts: last.id)]
        }
        return [retract("effort", effortId, reverts: last.id)]
    }

    // MARK: Reading

    func lastEvent(_ kind: String, _ id: String) -> Event? {
        state.events.last { $0.record.kind == kind && $0.record.id == id }
    }

    func wrote(_ kind: String, _ id: String) -> Bool {
        lastEvent(kind, id) != nil
    }

    /// The latest version of each record of a kind written here, in the order first written. Retracted ones
    /// are left out.
    func written<P: Decodable & Encodable & Hashable & Sendable>(_ kind: String, as type: P.Type) -> [Record<P>] {
        var order: [String] = []
        var latest: [String: P?] = [:]
        for event in state.events where event.record.kind == kind {
            if latest[event.record.id] == nil { order.append(event.record.id) }
            latest[event.record.id] = .some(event.decoded(P.self))
        }
        return order.compactMap { id in
            latest[id].flatMap { $0 }.map { Record(id: id, payload: $0, prev: prev(kind, id)) }
        }
    }

    public var activeSession: PerformedSession? {
        guard let id = state.activeSessionId else { return nil }
        return latestSessions[id]
    }

    /// The latest version of each session logged on this phone.
    var latestSessions: [String: PerformedSession] {
        Dictionary(written("session", as: SessionPayload.self).map { ($0.id, $0) }, uniquingKeysWith: { _, b in b })
    }

    /// Sessions logged on this phone, newest first.
    public var sessions: [PerformedSession] {
        latestSessions.values.sorted { $0.startedAt > $1.startedAt }
    }

    /// The latest version of each set this phone logged or corrected, in the order first logged.
    public var localEfforts: [PerformedEffort] {
        written("effort", as: EffortPayload.self)
    }

    /// This phone wrote the set's current version, so undo can take it back.
    public func canUndo(_ id: String) -> Bool {
        guard let last = lastEvent("effort", id) else { return false }
        return last.op == .put && last.reverts == nil
    }

    /// The set was first logged on another device.
    public func loggedElsewhere(_ id: String) -> Bool {
        state.remoteOriginals?[id] != nil
    }

    /// Ids of records of a kind this phone changed since the last pull, in the order first changed.
    func fresh(_ kind: String) -> [String] {
        let fetched = state.planFetchedAt.flatMap(Timestamp.date)
        let pending = Set(state.pending)
        var out: [String] = []
        for event in state.events where event.record.kind == kind {
            let isFresh = pending.contains(event.id)
                || fetched.map { (Timestamp.date(event.ts) ?? .distantPast) > $0 } ?? true
            if isFresh, !out.contains(event.record.id) { out.append(event.record.id) }
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
        for id in fresh("effort") {
            if current[id] == nil { order.append(id) }
            current[id] = local[id]
        }
        var out: [String: [Int: PerformedEffort]] = [:]
        for id in order {
            guard let effort = current[id], effort.plannedSessionId == planned.id, let pid = effort.plannedEffortId,
                  plannedIds.contains(pid) else { continue }
            out[pid, default: [:]][effort.round ?? 1] = effort
        }
        return out
    }

    // MARK: Settings

    /// Where the next session starts.
    mutating func setCurrentLocation(_ id: String?) {
        state.locationId = id
        put("setting", SettingId.currentLocation.rawValue, SettingPayload(value: id.map(JSONValue.string) ?? .null))
    }

    func setting(_ id: SettingId) -> JSONValue? {
        state.plan?.settings.first { $0.id == id.rawValue }?.value
    }

    // MARK: Sync

    /// Events waiting for the laptop, oldest first.
    public var pendingEvents: [Event] {
        let byId = Dictionary(state.events.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        return state.pending.compactMap { byId[$0] }
    }

    /// Record the laptop's answer to a push of `sent`. Whatever it kept is done; rejected events are set
    /// aside with the reason, since sending them again won't help.
    public mutating func applyPush(_ response: EventsResponse, sent: [Event]) {
        var done = Set<String>()
        for result in response.results where result.index >= 0 && result.index < sent.count {
            let id = sent[result.index].id
            done.insert(id)
            if result.status == .rejected {
                state.rejected.append(RejectedEvent(eventId: id, reason: result.reason, at: Timestamp.string(clock())))
            }
        }
        state.pending.removeAll { done.contains($0) }
    }

    /// Take a pulled plan. For records the laptop now has, its versions replace what this phone kept, unless
    /// this phone has changes to them still to send. Returns how many events go out again, because the laptop
    /// no longer holds them (it was restored from an older backup).
    @discardableResult
    public mutating func applyPlan(_ window: PlanWindow) -> Int {
        if let hlc = window.hlc, HLC.parse(hlc) != nil, hlc > (state.hlc ?? ""), hlc < HLC.ceiling {
            state.hlc = hlc  // what's written here next comes after what the laptop has seen
        }
        drop(window.erased ?? [])
        setAside(window.rejected ?? [])
        if let unsure = state.unsureAfter {
            if window.received?[state.deviceId] == nil, unsure > (state.resendAfter ?? "") { state.resendAfter = unsure }
            state.unsureAfter = nil
        }
        let resent = window.received.map { resend(after: $0[state.deviceId]) } ?? 0
        state.plan = window
        state.planFetchedAt = Timestamp.string(clock())
        let waiting = Set(pendingEvents.map { Self.key($0.record.kind, $0.record.id) })
        let pulled = window.activities.map { Self.key("activity", $0.id) } + window.locations.map { Self.key("location", $0.id) }
            + window.settings.map { Self.key("setting", $0.id) } + window.plans.map { Self.key("plan", $0.id) }
            + window.plannedSessions.map { Self.key("planned_session", $0.id) }
            + window.overlays.map { Self.key("overlay", $0.id) } + window.sessions.map { Self.key("session", $0.id) }
            + window.efforts.map { Self.key("effort", $0.id) }
        for key in pulled where !waiting.contains(key) { state.heads?[key] = nil }
        state.workouts = state.workouts?.filter { waiting.contains(Self.key("overlay", Self.rearrangeId($0.key))) }
        if !waiting.contains(Self.key("setting", SettingId.equipment.rawValue)) {
            if case .array(let items)? = setting(.equipment) {
                state.equipment = items.compactMap { if case .string(let s) = $0 { return s } else { return nil } }
            } else {
                state.equipment = nil
            }
        }
        if !waiting.contains(Self.key("setting", SettingId.currentLocation.rawValue)), activeSession == nil {
            if case .string(let id)? = setting(.currentLocation) { state.locationId = id } else { state.locationId = nil }
        }
        return resent
    }

    /// Queue again what the laptop no longer holds: this phone's events after `marker`, the latest it holds.
    mutating func resend(after marker: String?) -> Int {
        let floor = Swift.max(marker ?? "", state.resendAfter ?? "")
        let skip = Set(state.pending).union(state.rejected.map(\.eventId))
        let again = state.events.filter { !skip.contains($0.id) && $0.hlc > floor }
        guard !again.isEmpty else { return 0 }
        let queued = Set(state.pending).union(again.map(\.id))
        state.pending = state.events.map(\.id).filter { queued.contains($0) }
        var heads = state.heads ?? [:]
        for event in again {
            heads[Self.key(event.record.kind, event.record.id)] = lastEvent(event.record.kind, event.record.id).map { [$0.id] }
        }
        state.heads = heads
        return again.count
    }

    /// Note the store this phone now sends to. Another store keeps what it took; this one gets what is
    /// still waiting and what's logged from now on.
    public mutating func paired(storeId: String) {
        let pending = Set(state.pending)
        let last = state.events.last(where: { !pending.contains($0.id) })?.hlc ?? ""
        if state.storeId == nil {
            if last > (state.unsureAfter ?? "") { state.unsureAfter = last }
        } else if state.storeId != storeId {
            let floor = Swift.max(last, state.unsureAfter ?? "")
            if floor > (state.resendAfter ?? "") { state.resendAfter = floor }
            state.unsureAfter = nil
        }
        state.storeId = storeId
    }

    /// The laptop's record of events it refused, for when its answer to a push never arrived.
    mutating func setAside(_ refused: [Rejection]) {
        let held = Set(state.events.map(\.id)), known = Set(state.rejected.map(\.eventId))
        let refused = refused.filter { held.contains($0.id) }
        state.rejected += refused.filter { !known.contains($0.id) }
            .map { RejectedEvent(eventId: $0.id, reason: $0.reason, at: Timestamp.string(clock())) }
        let ids = Set(refused.map(\.id))
        state.pending.removeAll { ids.contains($0) }
    }

    /// Forget everything this phone holds of records that were erased, in one pass over its events.
    mutating func drop(_ erased: [ErasedRecord]) {
        guard !erased.isEmpty else { return }
        let records = Set(erased.map { RecordRef(kind: $0.kind, id: $0.id) })
        let gone = Set(state.events.lazy.filter { records.contains($0.record) }.map(\.id))
        if !gone.isEmpty {
            state.events.removeAll { gone.contains($0.id) }
            state.pending.removeAll { gone.contains($0) }
            state.rejected.removeAll { gone.contains($0.eventId) }
        }
        for record in records {
            let (kind, id) = (record.kind, record.id)
            state.heads?[Self.key(kind, id)] = nil
            switch kind {
            case "effort": state.remoteOriginals?[id] = nil
            case "session" where state.activeSessionId == id: state.activeSessionId = nil
            case "location" where state.locationId == id: state.locationId = nil
            case "planned_session": state.workouts?[id] = nil
            default: break
            }
        }
    }

    static func rearrangeId(_ plannedSessionId: String) -> String {
        DerivedId.overlay(.rearrange, kind: "planned_session", id: plannedSessionId)
    }
}

extension PlanWindow {
    public func activity(_ id: String) -> Activity? {
        activities.first { $0.id == id }
    }

    /// Planned sessions grouped by day, in date order.
    public var days: [(date: String, sessions: [PlannedSession])] {
        let grouped = Dictionary(grouping: plannedSessions, by: { $0.payload.scheduledOn })
        return grouped.keys.sorted().map { ($0, grouped[$0] ?? []) }
    }

    public func plannedSession(_ id: String?) -> PlannedSession? {
        id.flatMap { id in plannedSessions.first { $0.id == id } }
    }
}

extension Record where Payload == PlannedSessionPayload {
    /// How many planned efforts there are, counting each round.
    public var plannedCount: Int {
        payload.blocks.reduce(0) { $0 + ($1.rounds ?? 1) * $1.efforts.count }
    }
}
