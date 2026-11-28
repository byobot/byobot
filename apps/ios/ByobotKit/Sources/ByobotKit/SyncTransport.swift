// What the app's sync needs from wherever the byobot logbook lives: today the laptop over HTTPS.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

public protocol SyncTransport: Sendable {
    func push(events: [Event]) async throws -> EventsResponse
    func plan() async throws -> PlanWindow
    func push(samples: [Sample], deleted: [Deletion]) async throws -> SamplesResponse
}

/// The paired laptop, through `byobot serve`.
extension SyncClient: SyncTransport {}
