// One sync over every route this phone has: the paired laptop, then Drive. Each route takes what's still waiting.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Where the app keeps its logbook: a sync reads it and changes it through here, and each change is saved.
@MainActor
public protocol SyncBook: AnyObject {
    var book: Logbook { get }
    func change(_ body: (inout Logbook) -> Void)
}

/// One way to the Mac's logbook.
@MainActor
public protocol SyncRoute: AnyObject {
    /// Sends what's waiting and the Apple Health readings given, and takes in the other side's answer. `takePlan`
    /// is false once an earlier route brought a plan, which a route that may lag behind then leaves alone.
    func sync(_ book: any SyncBook, health: SampleBatch, takePlan: Bool) async -> RouteOutcome
}

public struct RouteOutcome {
    /// The plan it brought is in the logbook.
    public var planned = false
    /// The Apple Health readings it was given are its to deliver now.
    public var tookHealth = false
    /// Joined but not let in yet: the Mac hasn't admitted this phone.
    public var waiting = false
    /// The other side took this phone off this route.
    public var removed = false
    public var error: (any Error)?

    public init() {}
}

@MainActor
public enum Syncer {
    public struct Report {
        /// One for each route, in the order given.
        public var outcomes: [RouteOutcome]
        /// Every Apple Health reading and deletion went to a route, so the read can be marked done.
        public var healthDone: Bool
    }

    /// Syncs by each route in turn. Apple Health readings and deletions go by the first route that takes them: the
    /// Mac keeps a deletion that arrives before its sample.
    public static func run(_ routes: [any SyncRoute], book: any SyncBook, health: SampleBatch?) async -> Report {
        var outcomes: [RouteOutcome] = [], planned = false, taken = false
        for route in routes {
            let outcome = await route.sync(book, health: taken ? SampleBatch() : health ?? SampleBatch(),
                                           takePlan: !planned)
            taken = taken || outcome.tookHealth
            planned = planned || outcome.planned
            outcomes.append(outcome)
        }
        return Report(outcomes: outcomes, healthDone: health != nil && taken)
    }
}

/// The paired laptop, through `byobot serve`. Its plan always lands: it is the source of truth.
@MainActor
public final class HubRoute: SyncRoute {
    public let transport: any SyncTransport

    public init(_ transport: any SyncTransport) {
        self.transport = transport
    }

    /// Pull the plan, push what this phone logged, pull again if it pushed, then send Apple Health readings.
    /// The plan comes first: what the laptop says it holds shows what it lost (it was restored) only until
    /// this phone pushes something newer.
    public func sync(_ book: any SyncBook, health: SampleBatch, takePlan: Bool) async -> RouteOutcome {
        var outcome = RouteOutcome()
        do {
            try await pull(book)
            outcome.planned = true
            if !book.book.pendingEvents.isEmpty {
                try await push(book)
                try await pull(book)
            }
            let limit = CloudRelay.maxBatch
            for start in stride(from: 0, to: health.samples.count, by: limit) {
                let part = Array(health.samples[start..<min(start + limit, health.samples.count)])
                _ = try await transport.push(samples: part, deleted: [])
            }
            for start in stride(from: 0, to: health.deleted.count, by: limit) {
                let part = Array(health.deleted[start..<min(start + limit, health.deleted.count)])
                _ = try await transport.push(samples: [], deleted: part)
            }
            outcome.tookHealth = true
        } catch {
            outcome.error = error
        }
        return outcome
    }

    func pull(_ book: any SyncBook) async throws {
        let window = try await transport.plan()
        book.change {
            $0.applyPlan(window)
            $0.state.lastSyncAt = Timestamp.now()
        }
    }

    func push(_ book: any SyncBook) async throws {
        while true {
            let batch = Array(book.book.pendingEvents.prefix(CloudRelay.maxBatch))
            if batch.isEmpty { return }
            let before = book.book.state.pending.count
            let response = try await transport.push(events: batch)
            book.change { $0.applyPush(response, sent: batch) }
            if book.book.state.pending.count >= before { return }  // no progress; try again next sync
        }
    }
}
