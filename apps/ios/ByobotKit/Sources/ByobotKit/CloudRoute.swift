// The phone's route through the cloud folder, beside or instead of the paired laptop.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

public actor CloudRoute {
    public struct Result: Sendable {
        /// Events the Mac has read, so nothing need send them again.
        public var delivered: Set<String>
        public var plan: PlanWindow?
        public var refused: [RefusedBatch]
        public var removed: Bool
        /// False until the Mac admits this phone.
        public var joined: Bool
    }

    public private(set) var relay: CloudRelay
    let folder: any CloudFolder
    let identity: CloudIdentity
    let save: @Sendable (CloudRelay) async throws -> Void

    public init(relay: CloudRelay, folder: any CloudFolder, identity: CloudIdentity,
                save: @escaping @Sendable (CloudRelay) async throws -> Void) {
        self.relay = relay
        self.folder = folder
        self.identity = identity
        self.save = save
    }

    /// Source ids of samples the Mac hasn't read yet. A deletion of one of these goes this way, never by the laptop.
    public var unread: Set<String> { relay.unread }

    /// Hands the relay what's waiting, syncs once and says what the Mac has taken.
    public func sync(events: [Event], samples: [Sample] = [], deleted: [Deletion] = []) async throws -> Result {
        try await queue(events: events, samples: samples, deleted: deleted)
        return try await sync()
    }

    /// Hands the relay what's waiting and saves it, so it goes on a later sync even if this one fails.
    public func queue(events: [Event], samples: [Sample] = [], deleted: [Deletion] = []) async throws {
        relay.log(events)
        if !samples.isEmpty || !deleted.isEmpty { relay.send(SampleBatch(samples: samples, deleted: deleted)) }
        try await save(relay)
    }

    /// Syncs once and says what the Mac has taken.
    public func sync() async throws -> Result {
        var working = relay
        let report: CloudRelay.Report
        do {
            report = try await working.sync(folder, identity: identity, save: save)
            relay = working
        } catch {
            relay = working  // what it sent before failing stays sent
            throw error
        }
        let delivered = relay.takeDelivered()
        let refused = relay.refused
        try await save(relay)
        return Result(delivered: delivered, plan: relay.plan, refused: refused, removed: report.removed,
                      joined: relay.joined)
    }
}

/// The phone's route through Drive. It only reads a plan when no earlier route brought one, since it may lag.
@MainActor
public final class DriveRoute: SyncRoute {
    public let route: CloudRoute
    /// False once the phone left this relay, so a sync that started before then leaves it alone.
    let isCurrent: @MainActor () -> Bool

    public init(_ route: CloudRoute, isCurrent: @escaping @MainActor () -> Bool = { true }) {
        self.route = route
        self.isCurrent = isCurrent
    }

    public func unread() async -> Set<String> { await route.unread }

    public func sync(_ book: any SyncBook, health: SampleBatch, takePlan: Bool) async -> RouteOutcome {
        var outcome = RouteOutcome()
        guard isCurrent() else { return outcome }
        do {
            try await route.queue(events: book.book.pendingEvents, samples: health.samples, deleted: health.deleted)
            outcome.tookHealth = true
            let result = try await route.sync()
            book.change {
                $0.delivered(result.delivered)
                if takePlan, let plan = result.plan {
                    $0.applyPlan(plan)
                    $0.state.lastSyncAt = Timestamp.now()
                }
            }
            outcome.planned = takePlan && result.plan != nil
            outcome.waiting = !result.joined && !result.removed
            outcome.removed = result.removed
        } catch {
            outcome.error = error
        }
        return outcome
    }
}
