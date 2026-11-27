// What the app keeps on the phone, and the events it writes when you train.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Everything the app keeps on the phone. The laptop's store is the source of truth; the phone keeps
/// the plan it last pulled, the events the laptop doesn't have yet, and a short tail of the rest.
public struct AppState: Codable, Equatable, Sendable {
    /// The file's format. A file from a newer app is set aside, not read.
    public static let currentVersion = 1
    public var version = AppState.currentVersion
    /// This install's device id. Every event the app writes carries it.
    public var deviceId: String
    public var server: PairedServer?
    public var plan: PlanWindow?
    /// Events written on this phone, in the order written: what's waiting, and a short tail of what was sent.
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
    /// Sets logged elsewhere, as they stood before this phone first corrected them.
    public var remoteOriginals: [String: PerformedEffort]?
    /// Your order and swaps for each planned session, by its id, until a pull brings the laptop's.
    public var workouts: [String: WorkoutChanges]?
    /// The store that holds what this phone sent.
    public var storeId: String?
    /// The store each event went to: one after a span's `after` went to its store, until the next span. Nil
    /// until this phone first moves to another store.
    public var storeSpans: [StoreSpan]?
    /// Sample data to look around with (see `Demo`): nothing syncs, and leaving it starts over.
    public var demo: Bool?

    public init(deviceId: String = UUID7.make()) {
        self.deviceId = deviceId
        self.events = []
        self.pending = []
        self.rejected = []
    }

    enum CodingKeys: String, CodingKey {
        case version, server, plan, events, pending, rejected, hlc, workouts
        case deviceId = "device_id"
        case activeSessionId = "active_session_id"
        case lastSyncAt = "last_sync_at"
        case lastHealthSyncAt = "last_health_sync_at"
        case remoteOriginals = "remote_originals"
        case storeId = "store_id"
        case storeSpans = "store_spans"
        case demo
    }
}

public struct StoreSpan: Codable, Equatable, Hashable, Sendable {
    public var storeId: String
    public var after: String

    public init(storeId: String, after: String) {
        self.storeId = storeId
        self.after = after
    }

    enum CodingKeys: String, CodingKey {
        case after
        case storeId = "store_id"
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

    enum CodingKeys: String, CodingKey {
        case reason, at
        case eventId = "event_id"
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
    /// The library this app shipped, for activities the plan doesn't send.
    public var shipped = ActivityLibrary.bundled

    public init(state: AppState, actor: Actor = Actor(kind: .human, id: "me"), origin: Origin? = nil,
                clock: @escaping @Sendable () -> Date = { Date() }) {
        self.state = state
        self.actor = actor
        self.origin = origin
        self.clock = clock
    }

    // MARK: Writing
    // A write to a record whose version needs a newer app is left out (nil): it would drop what this app can't read.

    @discardableResult
    mutating func put<P: Codable & Hashable & Sendable>(_ kind: String, _ id: String, _ payload: P,
                                                        over base: Record<P>? = nil, known: [String]? = nil,
                                                        reverts: String? = nil) -> Event? {
        guard !isNewer(kind, id) else { return nil }
        var json = Payloads.json(payload)
        // A change to a record writes only what changed, so fields a newer byobot added stay.
        if let base, let raw = base.raw { json = Payloads.patch(raw, from: Payloads.json(base.payload), to: json) }
        return append(kind, id, .put, json, prev: prev(kind, id, known: known), reverts: reverts)
    }

    @discardableResult
    mutating func retract(_ kind: String, _ id: String, reverts: String? = nil) -> Event? {
        guard !isNewer(kind, id) else { return nil }
        return append(kind, id, .retract, .object([:]), prev: prev(kind, id), reverts: reverts)
    }

    mutating func append(_ kind: String, _ id: String, _ op: Op, _ payload: JSONValue, prev: [String],
                         reverts: String?) -> Event {
        let now = clock()
        let hlc = HLC.next(after: state.hlc, now: now)
        let event = Event(id: UUID7.make(at: now), schemaVersion: ContractVersion.schema,
                          record: RecordRef(kind: kind, id: id), op: op,
                          kindVersion: op == .put ? (KindVersion.all[kind] ?? 1) : nil, hlc: hlc, ts: Timestamp.string(now),
                          deviceId: state.deviceId, actor: actor, prev: prev, reverts: reverts,
                          payload: payload)
        state.hlc = hlc
        state.events.append(event)
        state.pending.append(event.id)
        return event
    }

    /// The events a change to the record names as `prev`: this phone's last write while the laptop hasn't shown
    /// it holds it, else the laptop's, else this phone's last write.
    func prev(_ kind: String, _ id: String, known: [String]? = nil) -> [String] {
        let mine = lastEvent(kind, id)
        if let mine, fresh(mine, pending: Set(state.pending)) { return [mine.id] }
        if let known { return known }
        if let entry = entry(kind, id) { return entry.prev }
        if let removed = state.plan?.removed.first(where: { $0.kind == kind && $0.id == id }) { return removed.prev }
        return mine.map { [$0.id] } ?? []
    }

    /// Whether the laptop's version of the record needs a newer app. Changing it here would drop what this app
    /// can't read, so the app leaves it as it is.
    public func isNewer(_ kind: String, _ id: String) -> Bool {
        if let mine = lastEvent(kind, id), fresh(mine, pending: Set(state.pending)) { return false }
        return (entry(kind, id)?.kindVersion ?? 1) > (KindVersion.all[kind] ?? 1)
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
        let location = planned?.locationId ?? pickedLocationId
        if location != pickedLocationId { setCurrentLocation(location) }
        let links = planned.map { p in
            Links(partOf: p.planId.map { Ref(kind: "plan", id: $0) }, answers: Ref(kind: "planned_session", id: p.id))
        }
        let id = UUID7.make(at: clock())
        let payload = SessionPayload(time: now(), origin: origin, status: .inProgress,
                                     links: links, name: planned?.name ?? name, locationId: location)
        let event = put("session", id, payload)
        state.activeSessionId = id
        return PerformedSession(id: id, payload: payload, prev: event.map { [$0.id] } ?? [])
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
        let payload = EffortPayload(time: now(), origin: origin, status: .completed,
                                    links: links, activityId: activityId, round: round, side: side,
                                    metrics: metrics.isEmpty ? nil : metrics, notes: notes?.isEmpty == true ? nil : notes)
        let event = put("effort", id, payload)
        return PerformedEffort(id: id, payload: payload, prev: event.map { [$0.id] } ?? [])
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

    /// Finish the active session: write it again with its end time and any note you typed.
    @discardableResult
    public mutating func finishSession(notes: String? = nil) -> PerformedSession? {
        guard var session = activeSession else { return nil }
        session.payload.time.end = Timestamp.string(clock())
        session.payload.status = .completed
        if let notes, !notes.isEmpty { session.payload.notes = notes }
        let event = put("session", session.id, session.payload, over: activeSession)
        state.activeSessionId = nil
        guard let event else { return nil }
        session.prev = [event.id]
        session.raw = event.payload
        return session
    }

    /// Correct a logged set from any device. Keeps its round; naming its current version as prev means a
    /// change made elsewhere at the same time shows as a conflict instead of being lost.
    @discardableResult
    public mutating func editEffort(_ original: PerformedEffort, metrics: Metrics, side: Side?, notes: String?,
                                    activityId: String? = nil, performedAt: String? = nil) -> PerformedEffort {
        let first = !wrote("effort", original.id)
        var effort = original
        effort.payload.metrics = metrics.isEmpty ? nil : metrics
        effort.payload.side = side
        effort.payload.notes = notes?.isEmpty == true ? nil : notes
        if let activityId, !activityId.isEmpty { effort.payload.activityId = activityId }
        if let performedAt {
            effort.payload.time.start = performedAt
            if let end = effort.payload.time.end.flatMap(Timestamp.date), let start = Timestamp.date(performedAt),
               end < start {
                effort.payload.time.end = nil
            }
        }
        guard let event = put("effort", original.id, effort.payload, over: original, known: original.prev) else {
            return original
        }
        if first {
            var originals = state.remoteOriginals ?? [:]
            originals[original.id] = original
            state.remoteOriginals = originals
        }
        effort.prev = [event.id]
        effort.raw = event.payload
        return effort
    }

    /// Take back what this phone wrote for a set: one logged here goes away, one corrected here goes back
    /// to how it was logged elsewhere.
    @discardableResult
    public mutating func undoEffort(id effortId: String) -> [Event] {
        guard canUndo(effortId), let last = lastEvent("effort", effortId) else { return [] }
        if let original = state.remoteOriginals?[effortId] {
            return put("effort", effortId, original.payload, over: original, reverts: last.id).map { [$0] } ?? []
        }
        return retract("effort", effortId, reverts: last.id).map { [$0] } ?? []
    }

    // MARK: Reading

    /// This phone's last event for the record that the laptop didn't refuse.
    func lastEvent(_ kind: String, _ id: String) -> Event? {
        let refused = Set(state.rejected.map(\.eventId))
        return state.events.last { $0.record.kind == kind && $0.record.id == id && !refused.contains($0.id) }
    }

    func wrote(_ kind: String, _ id: String) -> Bool {
        lastEvent(kind, id) != nil
    }

    /// The latest version of each record of a kind written here, in the order first written. Retracted ones
    /// are left out.
    func written<P: Decodable & Encodable & Hashable & Sendable>(_ kind: String, as type: P.Type,
                                                                 where keep: (JSONValue) -> Bool = { _ in true }) -> [Record<P>] {
        var order: [String] = []
        var latest: [String: Event] = [:]
        for event in state.events where event.record.kind == kind {
            if latest[event.record.id] == nil { order.append(event.record.id) }
            latest[event.record.id] = event
        }
        return order.compactMap { id in
            guard let event = latest[id], keep(event.payload) else { return nil }
            return event.decoded(P.self).map { Record(id: id, payload: $0, prev: [event.id], raw: event.payload) }
        }
    }

    /// The laptop hasn't shown it holds this event of this phone's: it is waiting, or the pulled plan's latest of
    /// this phone's events comes before it. Before the first plan, only waiting counts.
    func fresh(_ event: Event, pending: Set<String>) -> Bool {
        if pending.contains(event.id) { return true }
        guard let plan = state.plan else { return false }
        return event.hlc > (plan.received[state.deviceId] ?? "")
    }

    /// The records of a kind as this phone shows them, by one rule: the pulled ones, with this phone's latest
    /// version on top where it is fresh, and this phone's own records that the plan can't speak for (`covered`
    /// is false: the plan would carry them if the laptop still had them). `matches` skips this phone's events
    /// before they are decoded; a fresh one that doesn't match still hides the pulled version.
    func current<P: Codable & Hashable & Sendable>(_ kind: String, _ pulled: [Record<P>],
                                                    covered: (Record<P>) -> Bool = { _ in true },
                                                    matches: (JSONValue) -> Bool = { _ in true }) -> [Record<P>] {
        var order: [String] = [], byId: [String: Record<P>] = [:]
        for record in pulled {
            if byId[record.id] == nil { order.append(record.id) }
            byId[record.id] = record
        }
        let refused = Set(state.rejected.map(\.eventId)), pending = Set(state.pending)
        var mine: [String] = [], latest: [String: Event] = [:]
        for event in state.events where event.record.kind == kind && !refused.contains(event.id) {
            if latest[event.record.id] == nil { mine.append(event.record.id) }
            latest[event.record.id] = event
        }
        let says = state.plan != nil
        for id in mine {
            guard let event = latest[id] else { continue }
            let record: Record<P>? = matches(event.payload)
                ? event.decoded(P.self).map { Record(id: id, payload: $0, prev: [event.id], raw: event.payload) } : nil
            if fresh(event, pending: pending) {
                if byId[id] == nil { order.append(id) }
                byId[id] = record
            } else if byId[id] == nil, let record, !(says && covered(record)) {
                order.append(id)
                byId[id] = record
            }
        }
        return order.compactMap { byId[$0] }
    }

    /// Whether the plan carries the sets and sessions of a planned session, of a session logged against one, or of
    /// a day (`yyyy-mm-dd`) in its history.
    func carries(plannedSession: String?, session: String?, day: String? = nil) -> Bool {
        guard let plan = state.plan else { return false }
        if let plannedSession, plan.plannedSessions.contains(where: { $0.id == plannedSession }) { return true }
        if let session, plan.sessions.contains(where: { $0.id == session }) { return true }
        if let day, day >= plan.since, day <= plan.to { return true }
        return false
    }

    /// Sets as this phone shows them: the pulled ones with this phone's changes on top.
    func currentEfforts(matching: (JSONValue) -> Bool = { _ in true }) -> [PerformedEffort] {
        current("effort", state.plan?.efforts ?? [], covered: {
            carries(plannedSession: $0.plannedSessionId, session: $0.sessionId,
                    day: $0.sessionId == nil ? String($0.performedAt.prefix(10)) : nil)
        }, matches: matching)
    }

    public var activeSession: PerformedSession? {
        guard let id = state.activeSessionId else { return nil }
        return latestSessions[id]
    }

    /// The latest version of each session this phone knows of.
    var latestSessions: [String: PerformedSession] {
        let sessions = current("session", state.plan?.sessions ?? [], covered: {
            carries(plannedSession: $0.plannedSessionId, session: nil, day: String($0.startedAt.prefix(10)))
        })
        return Dictionary(sessions.map { ($0.id, $0) }, uniquingKeysWith: { _, b in b })
    }

    /// Sessions this phone knows of, newest first.
    public var sessions: [PerformedSession] {
        latestSessions.values.sorted {
            (Timestamp.date($0.startedAt) ?? .distantPast) > (Timestamp.date($1.startedAt) ?? .distantPast)
        }
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

    public func efforts(inSession id: String) -> [PerformedEffort] {
        currentEfforts().filter { $0.sessionId == id }
    }

    /// What has been logged against each planned effort and round, here and (from the last pull) anywhere else.
    public func logged(for planned: PlannedSession) -> [String: [Int: PerformedEffort]] {
        let plannedIds = Set(planned.blocks.flatMap { $0.efforts.map(\.id) })
        var out: [String: [Int: PerformedEffort]] = [:]
        for effort in currentEfforts() {
            guard effort.plannedSessionId == planned.id, let pid = effort.plannedEffortId,
                  plannedIds.contains(pid) else { continue }
            out[pid, default: [:]][effort.round ?? 1] = effort
        }
        return out
    }

    // MARK: Settings

    /// Where the next session starts.
    mutating func setCurrentLocation(_ id: String?) {
        put("setting", SettingId.currentLocation.rawValue, SettingPayload(value: id.map(JSONValue.string) ?? .null))
    }

    func setting(_ id: SettingId) -> JSONValue? {
        current("setting", state.plan?.settings ?? []).first { $0.id == id.rawValue }?.value
    }

    /// Where the next session starts: the last place you picked, unless it was erased.
    var pickedLocationId: String? {
        guard case .string(let id)? = setting(.currentLocation) else { return nil }
        let erased = state.plan?.erased ?? []
        return erased.contains { $0.kind == "location" && $0.id == id } ? nil : id
    }

    /// Equipment you have when your location has no list of its own. Nil means everything.
    var defaultEquipment: [String]? {
        guard case .array(let items)? = setting(.equipment) else { return nil }
        return items.compactMap { if case .string(let s) = $0 { return s } else { return nil } }
    }

    // MARK: Sync

    /// Events waiting for the laptop, oldest first.
    public var pendingEvents: [Event] {
        let byId = Dictionary(state.events.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        return state.pending.compactMap { byId[$0] }
    }

    /// Record the laptop's answer to a push of `sent`. Whatever it kept is done; rejected events are set
    /// aside with the reason, since sending them again won't help. An answer this app doesn't know is retried.
    public mutating func applyPush(_ response: EventsResponse, sent: [Event]) {
        var done = Set<String>()
        for result in response.results where result.index >= 0 && result.index < sent.count {
            if case .other = result.status { continue }
            let id = sent[result.index].id
            done.insert(id)
            if result.status == .rejected {
                state.rejected.append(RejectedEvent(eventId: id, reason: result.reason, at: Timestamp.string(clock())))
            }
        }
        state.pending.removeAll { done.contains($0) }
        prune()
    }

    /// Take a pulled plan. Its versions of records replace what this phone kept, unless this phone's are fresh.
    /// Returns how many events go out again, because the laptop no longer holds them (it was restored from an
    /// older backup).
    @discardableResult
    public mutating func applyPlan(_ window: PlanWindow) -> Int {
        if HLC.parse(window.hlc) != nil, window.hlc > (state.hlc ?? ""), window.hlc < HLC.ceiling {
            state.hlc = window.hlc  // what's written here next comes after what the laptop has seen
        }
        drop(window.erased)
        setAside(window.rejected)
        let resent = resend(after: window.received[state.deviceId])
        state.plan = window
        let planned = Set(window.plannedSessions.map(\.id))
        state.workouts = state.workouts?.filter { planned.contains($0.key) }
        prune()
        return resent
    }

    /// Queue again what the laptop no longer holds: this phone's events after `marker`, the latest it holds, that
    /// went to its store.
    mutating func resend(after marker: String?) -> Int {
        let floor = marker ?? ""
        let skip = Set(state.pending).union(state.rejected.map(\.eventId))
        let again = state.events.filter { !skip.contains($0.id) && $0.hlc > floor && store(of: $0) == state.storeId }
        guard !again.isEmpty else { return 0 }
        let queued = Set(state.pending).union(again.map(\.id))
        state.pending = state.events.map(\.id).filter { queued.contains($0) }
        return again.count
    }

    /// The store an event went to, as far as this phone knows.
    func store(of event: Event) -> String? {
        guard let spans = state.storeSpans, !spans.isEmpty else { return state.storeId }
        return spans.last(where: { $0.after < event.hlc })?.storeId ?? spans[0].storeId
    }

    /// Note the store this phone now sends to. Another store keeps what it took; this one gets what is
    /// still waiting and what's logged from now on, and, when it is a store this phone sent to before, what it
    /// took then and has lost since.
    public mutating func paired(storeId: String) {
        let pending = Set(state.pending)
        let last = state.events.last(where: { !pending.contains($0.id) })?.hlc ?? ""
        if let old = state.storeId, old != storeId {
            var spans = state.storeSpans ?? [StoreSpan(storeId: old, after: "")]
            spans.append(StoreSpan(storeId: storeId, after: Swift.max(last, spans.last?.after ?? "")))
            state.storeSpans = spans
        }
        state.storeId = storeId
    }

    /// Before leaving a store: what it says it refused and holds of what is waiting, for when its answer to a push
    /// never arrived. Pushes go out in the order written, so what is waiting up to the latest event it holds was taken.
    /// Events the Mac read from the cloud folder: done, whichever route they went by.
    public mutating func delivered(_ ids: Set<String>) {
        state.pending.removeAll { ids.contains($0) }
        prune()
    }

    public mutating func settle(with window: PlanWindow) {
        setAside(window.rejected)
        guard let held = window.received[state.deviceId] else { return }
        let taken = Set(pendingEvents.lazy.filter { $0.hlc <= held }.map(\.id))
        state.pending.removeAll { taken.contains($0) }
        prune()
    }

    /// Sent events kept for undo and for a laptop restored from a backup, at most this many from the last month.
    static let keptSent = 1000
    /// Refusals kept to show, the newest; the laptop keeps its own list too.
    static let keptRejected = 200

    /// Keeps what the phone still needs of its events: what's waiting or refused, the active session's, the newest
    /// sent (it marks how far this phone sent), and the last month of the rest. The pulled plan has the others.
    mutating func prune() {
        if state.rejected.count > Self.keptRejected {
            let gone = Set(state.rejected.dropLast(Self.keptRejected).map(\.eventId))
            state.rejected.removeFirst(state.rejected.count - Self.keptRejected)
            state.events.removeAll { gone.contains($0.id) }
        }
        let keep = Set(state.pending).union(state.rejected.map(\.eventId))
        let monthAgo = clock().addingTimeInterval(-30 * 86_400).timeIntervalSince1970
        let floor = HLC.format(ms: UInt64(Swift.max(0, monthAgo) * 1000), counter: 0)
        var kept = [Bool](repeating: false, count: state.events.count)
        var sent = 0, newest = true
        for i in state.events.indices.reversed() {
            let event = state.events[i]
            if keep.contains(event.id) || state.activeSessionId.map({ Self.belongs(event, to: $0) }) == true {
                kept[i] = true
            } else if newest || (sent < Self.keptSent && event.hlc >= floor) {
                kept[i] = true
                sent += 1
            }
            if !keep.contains(event.id) { newest = false }
        }
        guard kept.contains(false) else { return }
        state.events = zip(state.events, kept).filter { $0.1 }.map { $0.0 }
        if let originals = state.remoteOriginals {
            let held = Set(state.events.lazy.filter { $0.record.kind == "effort" }.map(\.record.id))
            let left = originals.filter { held.contains($0.key) }
            state.remoteOriginals = left.isEmpty ? nil : left
        }
    }

    /// The event writes the session, or a set logged in it.
    static func belongs(_ event: Event, to session: String) -> Bool {
        if event.record.kind == "session" { return event.record.id == session }
        guard case .object(let payload) = event.payload, case .object(let links)? = payload["links"],
              case .object(let partOf)? = links["part_of"], case .string(let id)? = partOf["id"] else { return false }
        return id == session
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
            switch kind {
            case "effort": state.remoteOriginals?[id] = nil
            case "session" where state.activeSessionId == id: state.activeSessionId = nil
            case "planned_session": state.workouts?[id] = nil
            default: break
            }
        }
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
