// What the app's sync needs from wherever the byobot logbook lives: today the laptop over HTTPS.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

public protocol SyncTransport: Sendable {
    /// The store this phone's events go to.
    func storeId() async throws -> String
    func push(events: [Event]) async throws -> EventsResponse
    func plan(from: String?, to: String?) async throws -> PlanWindow
    func push(samples: [Sample], deleted: [Deletion]) async throws -> SamplesResponse
}

extension SyncTransport {
    public func plan() async throws -> PlanWindow {
        try await plan(from: nil, to: nil)
    }
}

/// The paired laptop, through `byobot serve`.
extension SyncClient: SyncTransport {
    public func storeId() async throws -> String {
        try await hello().server.storeId
    }
}
