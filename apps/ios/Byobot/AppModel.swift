// The app's state and the things it does: pair, sync, log. Views call these and read `book`.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import Foundation
import SwiftUI
import UIKit

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var book: Logbook
    @Published private(set) var syncing = false
    @Published var message: String?
    /// A pairing link the app was opened with, waiting for the person to confirm.
    @Published var pendingLink: PairingLink?
    /// The rest between sets, counting down.
    @Published var rest: Countdown?
    /// An interval block or a hold running on the clock.
    @Published var clock: RunningClock?
    private var clockPhase: Int?
    /// Apple Health has types byobot reads that you haven't been asked about yet.
    @Published var healthNeedsAccess = false
    @Published var healthEnabled: Bool {
        didSet { UserDefaults.standard.set(healthEnabled, forKey: "healthEnabled") }
    }

    let health = HealthKitReader()
    private let file: URL

    init() {
        file = AppModel.stateFile()
        let state = (try? Data(contentsOf: file)).flatMap { try? JSONDecoder().decode(AppState.self, from: $0) }
        book = Logbook(state: state ?? AppState(), origin: Origin(app: "byobot_ios", version: AppModel.version,
                                                                   device: UIDevice.current.name))
        healthEnabled = UserDefaults.standard.bool(forKey: "healthEnabled")
        if state == nil { save() }
        keepAwake()
    }

    static var version: String? { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String }

    var state: AppState { book.state }
    var plan: PlanWindow? { book.state.plan }
    var server: PairedServer? { book.state.server }

    // MARK: Saving

    /// The state lives in Application Support, excluded from device backups: health data stays on
    /// the phone and your laptop, never in iCloud.
    static func stateFile() -> URL {
        let fm = FileManager.default
        var dir = (try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? fm.temporaryDirectory
        dir.appendPathComponent("byobot", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? dir.setResourceValues(values)
        return dir.appendingPathComponent("state.json")
    }

    func save() {
        do {
            let data = try JSONEncoder().encode(book.state)
            try data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch {
            message = "Couldn't save on this phone: \(error.localizedDescription)"
        }
    }

    private func change(_ body: (inout Logbook) -> Void) {
        body(&book)
        save()
        keepAwake()
    }

    /// The screen stays on while a session runs, so the timers and the next set stay in view.
    private func keepAwake() {
        UIApplication.shared.isIdleTimerDisabled = book.activeSession != nil
    }

    // MARK: Pairing

    func open(_ url: URL) {
        do {
            pendingLink = try PairingLink(link: url.absoluteString)
        } catch {
            message = "That link isn't a byobot pairing link this version understands."
        }
    }

    /// Look at a laptop before pairing by hand: whom it says it is, and the certificate it presents.
    func probe(host: String, port: Int) async throws -> (hello: SyncHello, fingerprint: String) {
        let client = SyncClient(hosts: [host], port: port, fingerprint: nil)
        let hello = try await client.hello()
        guard let seen = client.seenFingerprint else { throw SyncClientError.badResponse("no certificate") }
        return (hello, seen)
    }

    func pair(hosts: [String], port: Int, code: String, fingerprint: String) async throws {
        let client = SyncClient(hosts: hosts, port: port, fingerprint: fingerprint)
        let response = try await client.pair(code: code, deviceId: state.deviceId, name: UIDevice.current.name)
        try Keychain.setToken(response.token, for: response.server.deviceId)
        if let old = server, old.deviceId != response.server.deviceId {
            Keychain.deleteToken(for: old.deviceId)
        }
        change {
            $0.paired(storeId: response.server.storeId)
            $0.state.server = PairedServer(hosts: hosts, port: port, fingerprint: fingerprint,
                                           deviceId: response.server.deviceId, name: response.server.name,
                                           pairedAt: response.device.pairedAt)
        }
        pendingLink = nil
        await sync()
    }

    /// Forget the laptop. What you logged stays on the phone and goes to whichever laptop you pair next.
    func forgetServer() {
        guard let server else { return }
        Keychain.deleteToken(for: server.deviceId)
        change {
            $0.state.server = nil
            $0.state.plan = nil
            $0.state.planFetchedAt = nil
        }
    }

    // MARK: Sync

    /// Push what this phone logged, then pull the plan, then send new body-mass readings.
    func sync() async {
        guard !syncing, let server else { return }
        guard let token = Keychain.token(for: server.deviceId) else {
            message = SyncClientError.unauthorized.localizedDescription
            return
        }
        syncing = true
        defer { syncing = false }
        let client = SyncClient(server: server, token: token)
        do {
            if book.state.storeId == nil {  // paired before the app kept it
                let hello = try await client.hello()
                change { $0.state.storeId = hello.server.storeId }
            }
            // A plan can ask for events again (the laptop was restored); those go out before this sync ends.
            for _ in 0..<2 {
                try await pushPending(client)
                let window = try await client.plan()
                var resent = 0
                change {
                    resent = $0.applyPlan(window)
                    $0.state.lastSyncAt = Timestamp.now()
                }
                if resent == 0 { break }
            }
            if healthEnabled { try await syncHealth(client) }
            message = nil
        } catch {
            message = error.localizedDescription
        }
    }

    private func pushPending(_ client: SyncClient) async throws {
        while true {
            let batch = Array(book.pendingEvents.prefix(500))
            if batch.isEmpty { break }
            let before = book.state.pending.count
            let response = try await client.push(events: batch)
            change { $0.applyPush(response, sent: batch) }
            if book.state.pending.count >= before { break }  // no progress; try again next sync
        }
    }

    /// Bumped when the app starts reading more of Apple Health, so the next sync looks back a full year.
    private static let healthReads = 3

    private func syncHealth(_ client: SyncClient) async throws {
        guard health.available else { return }
        healthNeedsAccess = await health.needsAccess()
        // Look back a month past the last send, so readings an app wrote late still arrive.
        // Sending a reading twice changes nothing on the laptop.
        let current = UserDefaults.standard.integer(forKey: "healthReads") >= Self.healthReads
        let last = current ? state.lastHealthSyncAt.flatMap(Timestamp.date) : nil
        let now = Date()
        let yearAgo = now.addingTimeInterval(-365 * 86_400)
        let anchors = current ? (UserDefaults.standard.dictionary(forKey: "healthAnchors") as? [String: Data] ?? [:]) : [:]
        let changes = try await health.changes(since: yearAgo, anchors: anchors)
        // Days still being counted are read again from a month back, so totals an app wrote late still arrive.
        let from = last?.addingTimeInterval(-30 * 86_400) ?? yearAgo
        let days = try await health.dailyTotals(since: from).compactMap { HealthMapping.sample($0, readAt: now) }
        let samples = changes.readings.compactMap { HealthMapping.sample($0) } + days
        let deleted = changes.deleted.map { HealthMapping.deletion($0.0, type: $0.1) }
        for start in stride(from: 0, to: samples.count, by: 1000) {
            _ = try await client.push(samples: Array(samples[start..<min(start + 1000, samples.count)]))
        }
        for start in stride(from: 0, to: deleted.count, by: 1000) {
            _ = try await client.push(samples: [], deleted: Array(deleted[start..<min(start + 1000, deleted.count)]))
        }
        UserDefaults.standard.set(changes.anchors, forKey: "healthAnchors")
        UserDefaults.standard.set(Self.healthReads, forKey: "healthReads")
        change { $0.state.lastHealthSyncAt = Timestamp.now() }
    }

    func enableHealth() async {
        do {
            try await health.requestAccess()
            healthEnabled = true
            healthNeedsAccess = false
            await sync()
        } catch {
            message = error.localizedDescription
        }
    }

    // MARK: Logging

    func start(_ planned: PlannedSession?, name: String? = nil) {
        change { $0.startSession(planned: planned, name: name) }
    }

    func log(activityId: String, metrics: Metrics, planned: PlannedEffort? = nil, round: Int? = nil, side: Side? = nil,
             notes: String? = nil) {
        change {
            $0.logEffort(activityId: activityId, metrics: metrics, plannedEffortId: planned?.id, round: round,
                         side: side ?? planned?.side, notes: notes)
        }
    }

    func edit(_ effort: PerformedEffort, metrics: Metrics, side: Side?, notes: String?, activityId: String? = nil,
              performedAt: String? = nil) {
        change {
            $0.editEffort(effort, metrics: metrics, side: side, notes: notes, activityId: activityId, performedAt: performedAt)
        }
    }

    func undo(_ effort: PerformedEffort) {
        change { $0.undoEffort(id: effort.id) }
    }

    @discardableResult
    func finish(notes: String? = nil) -> PerformedSession? {
        var finished: PerformedSession?
        change { finished = $0.finishSession(notes: notes) }
        rest = nil
        stopClock()
        Task { await sync() }
        return finished
    }

    // MARK: Working through a plan

    /// Log a planned set, swap and all, then start the rest that follows it.
    func log(_ slot: Slot, metrics: Metrics, notes: String? = nil, in planned: PlannedSession) {
        let workout = book.workout(planned)
        change {
            $0.logEffort(activityId: slot.activityId, metrics: metrics, plannedEffortId: slot.effort.id, round: slot.round,
                         side: slot.effort.side, notes: workout.notes(for: slot, notes))
        }
        if clock == nil, let seconds = slot.restSeconds, seconds > 0, book.workout(planned).next != nil {
            rest = Countdown(startedAt: Date(), seconds: seconds)
        } else {
            rest = nil
        }
    }

    func setOrder(_ effortIds: [String], for planned: PlannedSession) {
        change { $0.setOrder(effortIds, for: planned.id) }
    }

    func swap(_ effortId: String, to swap: Swap?, for planned: PlannedSession) {
        change { $0.swap(effortId, to: swap, for: planned.id) }
    }

    func setEquipment(_ items: Set<String>?) {
        change { $0.setEquipment(items) }
    }

    func setLocation(_ id: String?) {
        change { $0.setLocation(id) }
    }

    func addLocation(name: String) {
        change { $0.addLocation(name: name) }
    }

    func startClock(_ slots: [Slot], for planned: PlannedSession) {
        rest = nil
        clock = RunningClock(plannedSessionId: planned.id, slotIds: slots.map(\.id), startedAt: Date())
        clockPhase = 0
        Haptics.phase()
    }

    func stopClock() {
        clock = nil
        clockPhase = nil
    }

    /// Move the timers on: buzz when rest ends or a phase changes, and log clock work once it's done.
    func tick(now: Date = Date()) {
        if let rest, rest.isDone(at: now) {
            self.rest = nil
            Haptics.done()
        }
        guard let running = clock, let timer = intervalClock(running) else { return }
        let elapsed = now.timeIntervalSince(running.startedAt)
        if let planned = plan?.plannedSessions.first(where: { $0.id == running.plannedSessionId }) {
            let workout = book.workout(planned)
            for slot in timer.finishedSlots(elapsed: elapsed) where slot.logged == nil {
                let metrics = book.asPlanned(slot, in: workout)
                change {
                    $0.logEffort(activityId: slot.activityId, metrics: metrics, plannedEffortId: slot.effort.id,
                                 round: slot.round, side: slot.effort.side, notes: workout.notes(for: slot, nil))
                }
            }
        }
        let phase = timer.position(elapsed: elapsed)?.index
        guard phase != clockPhase else { return }
        clockPhase = phase
        if phase == nil {
            stopClock()
            Haptics.done()
        } else {
            Haptics.phase()
        }
    }

    /// The running clock's phases, from the slots it started with as they stand now.
    func intervalClock(_ running: RunningClock) -> IntervalClock? {
        guard let planned = plan?.plannedSessions.first(where: { $0.id == running.plannedSessionId }) else { return nil }
        let byId = Dictionary(book.workout(planned).slots.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        return IntervalClock(slots: running.slotIds.compactMap { byId[$0] })
    }
}

struct RunningClock: Equatable {
    var plannedSessionId: String
    var slotIds: [String]
    var startedAt: Date
}

enum Haptics {
    static func done() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func phase() { UIImpactFeedbackGenerator(style: .heavy).impactOccurred() }
}
