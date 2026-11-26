// The phone's route through the cloud folder, beside or instead of the paired laptop.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

#if canImport(CryptoKit) && canImport(Compression)
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
        relay.log(events)
        if !samples.isEmpty || !deleted.isEmpty { relay.send(SampleBatch(samples: samples, deleted: deleted)) }
        try await save(relay)
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
#endif
