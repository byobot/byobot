// One sync with the paired laptop: pull the plan, push what's waiting, then send Apple Health readings.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Where the app keeps its logbook: a sync reads it and changes it through here, and each change is saved.
@MainActor
public protocol SyncBook: AnyObject {
    var book: Logbook { get }
    func change(_ body: (inout Logbook) -> Void)
}

/// Apple Health readings and deletions to send.
public struct SampleBatch: Equatable, Sendable {
    public var samples: [Sample]
    public var deleted: [Deletion]

    public init(samples: [Sample] = [], deleted: [Deletion] = []) {
        self.samples = samples
        self.deleted = deleted
    }
}

/// The paired laptop, through `byobot serve`. Its plan always lands: it is the source of truth.
@MainActor
public struct HubSync {
    /// The most events, or readings and deletions, the laptop takes in one request.
    public static let maxBatch = 2000

    public let transport: any SyncTransport

    public init(_ transport: any SyncTransport) {
        self.transport = transport
    }

    /// Pull the plan, push what this phone logged, pull again if it pushed, then send Apple Health readings.
    /// The plan comes first: what the laptop says it holds shows what it lost (it was restored) only until
    /// this phone pushes something newer. The readings went out only if this returns without throwing.
    public func sync(_ book: any SyncBook, health: SampleBatch) async throws {
        try await pull(book)
        if !book.book.pendingEvents.isEmpty {
            try await push(book)
            try await pull(book)
        }
        let limit = Self.maxBatch
        for start in stride(from: 0, to: health.samples.count, by: limit) {
            let part = Array(health.samples[start..<min(start + limit, health.samples.count)])
            _ = try await transport.push(samples: part, deleted: [])
        }
        for start in stride(from: 0, to: health.deleted.count, by: limit) {
            let part = Array(health.deleted[start..<min(start + limit, health.deleted.count)])
            _ = try await transport.push(samples: [], deleted: part)
        }
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
            let batch = Array(book.book.pendingEvents.prefix(Self.maxBatch))
            if batch.isEmpty { return }
            let before = book.book.state.pending.count
            let response = try await transport.push(events: batch)
            book.change { $0.applyPush(response, sent: batch) }
            if book.book.state.pending.count >= before { return }  // no progress; try again next sync
        }
    }
}
