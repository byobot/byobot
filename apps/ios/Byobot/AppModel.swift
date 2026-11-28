// The app's state and the things it does: pair, sync, log. Views call these and read `book`.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import Foundation
import SwiftUI
import UIKit

@MainActor
final class AppModel: ObservableObject, SyncBook {
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
    var healthEnabled: Bool { state.health?.enabled == true }

    let health = HealthKitReader()
    private let file: StateFile

    init() {
        var file = StateFile(url: AppModel.stateFile())
        let (state, outcome) = file.load()
        self.file = file
        book = Logbook(state: state, origin: Origin(app: "byobot_ios", version: AppModel.version,
                                                    device: UIDevice.current.name))
        switch outcome {
        case .loaded: break
        case .new: save()
        case .setAside(let aside):
            save()
            message = "This version couldn't read what the app kept, so it was set aside as \(aside.lastPathComponent) "
                + "and the app started over. Pair again; a later update may read it."
        case .cantRead: message = StateFile.Locked().errorDescription
        }
        keepAwake()
    }

    static var version: String? { Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String }

    var state: AppState { book.state }
    var plan: PlanWindow? { book.state.plan }
    var server: PairedServer? { book.state.server }
    /// Paired with a laptop, or looking around the demo.
    var connected: Bool { server != nil || demo }
    var demo: Bool { book.state.demo == true }

    /// Sample data to look around with, such as for App Review. Nothing syncs.
    func startDemo() {
        change {
            $0.state.demo = true
            $0.applyPlan(Demo.window(today: LocalDate.today()))
        }
    }

    /// Back to pairing, keeping nothing from the demo.
    func leaveDemo() {
        rest = nil
        stopClock()
        let health = state.health
        change {
            $0.state = AppState()
            $0.state.health = health
        }
    }

    // MARK: Saving

    /// The state lives in Application Support, the App Group's when the build names one, excluded from device
    /// backups: health data stays on the phone and your laptop, never in iCloud.
    nonisolated static func stateFile() -> URL {
        let fm = FileManager.default
        var dir = groupDirectory()
            ?? (try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? fm.temporaryDirectory
        dir.appendPathComponent("byobot", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? dir.setResourceValues(values)
        return dir.appendingPathComponent("state.json")
    }

    /// Application Support in the App Group set by `BYOBOT_APP_GROUP`, so a widget or watch app can read it.
    nonisolated static func groupDirectory() -> URL? {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "ByobotAppGroup") as? String, !group.isEmpty,
              let container = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group) else {
            return nil
        }
        return container.appendingPathComponent("Library/Application Support", isDirectory: true)
    }

    func save() {
        do {
            try file.save(book.state, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch let locked as StateFile.Locked {
            message = locked.errorDescription
        } catch {
            message = "Couldn't save on this phone: \(error.localizedDescription)"
        }
    }

    func change(_ body: (inout Logbook) -> Void) {
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
        let link: PairingLink
        do {
            link = try PairingLink(link: url.absoluteString)
        } catch {
            message = "That link isn't a byobot pairing link this version understands."
            return
        }
        guard let local = link.localOnly else {
            message = "That pairing link points outside your network. Pair only with a laptop on your own Wi-Fi."
            return
        }
        pendingLink = local
    }

    /// Look at a laptop before pairing by hand: whom it says it is, and the certificate it presents.
    func probe(host: String, port: Int) async throws -> (hello: SyncHello, fingerprint: String) {
        let client = SyncClient(hosts: [host], port: port, fingerprint: nil)
        let hello = try await client.hello()
        guard let seen = client.seenFingerprint else { throw SyncClientError.badResponse("no certificate") }
        return (hello, seen)
    }

    func pair(hosts: [String], port: Int, code: String, fingerprint: String) async throws {
        if demo { leaveDemo() }
        let client = SyncClient(hosts: hosts, port: port, fingerprint: fingerprint)
        leaving += 1
        do {
            defer { leaving -= 1 }
            let response = try await client.pair(code: code, deviceId: state.deviceId, name: UIDevice.current.name)
            await finishSyncing()
            let old = server, oldToken = old.flatMap { Keychain.token(for: $0.deviceId) }
            try Keychain.setToken(response.token, for: response.server.deviceId)
            if let old, book.state.storeId != response.server.storeId {
                await settle(leaving: old, token: oldToken)
            }
            if let old = server, old.deviceId != response.server.deviceId {
                Keychain.deleteToken(for: old.deviceId)
            }
            change {
                $0.paired(storeId: response.server.storeId)
                $0.state.server = PairedServer(hosts: hosts, port: port, fingerprint: fingerprint,
                                               deviceId: response.server.deviceId, name: response.server.name,
                                               pairedAt: response.device.pairedAt)
            }
        }
        pendingLink = nil
        await sync()
    }

    /// Forget the laptop. What you logged stays on the phone and goes to whichever laptop you pair next.
    func forgetServer() async {
        guard let server else { return }
        leaving += 1
        defer { leaving -= 1 }
        await finishSyncing()
        await settle(leaving: server, token: Keychain.token(for: server.deviceId))
        guard self.server == server else { return }
        Keychain.deleteToken(for: server.deviceId)
        change {
            $0.state.server = nil
            $0.state.plan = nil
        }
    }

    /// Pairings and forgets under way, so no sync starts against the laptop being left.
    private var leaving = 0

    /// Let a sync still talking to the old laptop land its answer first, or its pushes would go to the next one too.
    private func finishSyncing() async {
        await running?.value
    }

    /// Before leaving a laptop: what it took of what's waiting, for when its answer to a push was lost, so the
    /// next store doesn't get it too. Gives up after a few seconds; then the next store gets it.
    private func settle(leaving old: PairedServer, token: String?) async {
        guard !book.pendingEvents.isEmpty, let token else { return }
        let client = SyncClient(hosts: old.hosts, port: old.port, fingerprint: old.fingerprint, token: token, timeout: 3)
        let window = await withTaskGroup(of: PlanWindow?.self) { group in
            group.addTask { try? await client.plan(from: nil, to: nil) }
            group.addTask {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                return nil
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first
        }
        if let window { change { $0.settle(with: window) } }
    }

    // MARK: Sync

    private var running: Task<Void, Never>?
    private var syncAgain = false

    /// Sync with the paired laptop.
    func sync() async {
        guard leaving == 0 else { return }
        if let running {
            syncAgain = true
            await running.value
            return
        }
        guard server != nil else { return }
        let task = Task { await self.syncLoop() }
        running = task
        await task.value
    }

    private func syncLoop() async {
        syncing = true
        repeat {
            syncAgain = false
            await syncOnce()
        } while syncAgain && leaving == 0
        syncing = false
        running = nil
    }

    private func syncOnce() async {
        var healthError: String?, laptopError: String?
        var read: HealthRead?
        if healthEnabled {
            do {
                read = try await readHealth()
            } catch {
                healthError = error.localizedDescription
            }
        }
        if let server {
            if let token = Keychain.token(for: server.deviceId) {
                do {
                    let health = SampleBatch(samples: read?.samples ?? [], deleted: read?.deleted ?? [])
                    try await HubSync(SyncClient(server: server, token: token)).sync(self, health: health)
                    if let read {
                        change {
                            var kept = $0.state.health ?? HealthState()
                            kept.anchors = read.anchors
                            kept.days = read.days
                            $0.state.health = kept
                            $0.state.lastHealthSyncAt = Timestamp.now()
                        }
                    }
                } catch {
                    laptopError = error.localizedDescription
                }
            } else {
                laptopError = SyncClientError.unauthorized.localizedDescription
            }
        }
        message = healthError ?? laptopError
    }

    /// What Apple Health has new since the last send, with the anchors to keep once it's sent.
    private struct HealthRead {
        var samples: [Sample]
        var deleted: [Deletion]
        var anchors: [String: Data]
        /// Each daily total sent, with the offset its day started at.
        var days: [String: Int]
    }

    private func readHealth() async throws -> HealthRead? {
        guard health.available else { return nil }
        healthNeedsAccess = await health.needsAccess()
        // Look back a month past the last send, so readings an app wrote late still arrive.
        // Sending a reading twice changes nothing on the laptop.
        let last = state.lastHealthSyncAt.flatMap(Timestamp.date)
        let now = Date()
        let yearAgo = now.addingTimeInterval(-365 * 86_400)
        let anchors = state.health?.anchors ?? [:]
        let changes = try await health.changes(since: yearAgo, anchors: anchors)
        // Days still being counted are read again from a month back, so totals an app wrote late still arrive.
        let from = last?.addingTimeInterval(-30 * 86_400) ?? yearAgo
        let sent = state.health?.days ?? [:]
        let totals = try await health.dailyTotals(since: from)
        let days = HealthMapping.days(totals, since: from, sent: sent, readAt: now)
        return HealthRead(samples: changes.readings.compactMap { HealthMapping.sample($0) } + days.samples,
                          deleted: changes.deleted.map { HealthMapping.deletion($0.0, type: $0.1) } + days.deleted,
                          anchors: changes.anchors, days: days.sent)
    }

    func enableHealth() async {
        do {
            try await health.requestAccess()
            change {
                var health = $0.state.health ?? HealthState()
                health.enabled = true
                $0.state.health = health
            }
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

    func resolve(_ conflict: Conflict, keeping version: ConflictVersion) {
        change { $0.resolve(conflict, keeping: version) }
        Task { await sync() }
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

