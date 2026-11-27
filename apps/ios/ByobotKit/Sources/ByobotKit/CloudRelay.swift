// The phone as a relay in cloud protocol 1: it sends what it logged through the cloud folder and reads the keeper's
// plan. It follows python/tests/cloud_doubles.py.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import CryptoKit
import Foundation

/// A file is written once; another with the same path is already there.
public struct CloudExists: Error, Equatable {
    public let path: String
    public init(_ path: String) { self.path = path }
}

/// Where the cloud files live: the store's folder in Drive, or one in memory for tests.
public protocol CloudFolder: Sendable {
    /// Every file under the store's folder: path -> checksum.
    func list() async throws -> [String: String]
    /// A file's bytes, or nil if it's gone.
    func read(_ path: String) async throws -> Data?
    /// Writes a new file; throws CloudExists if the path is taken.
    func create(_ path: String, _ data: Data) async throws
    /// Deletes outright. A file already gone is fine.
    func delete(_ path: String) async throws
}

/// What a `byobot://pair` link holds for joining a Drive store.
public struct CloudJoin: Equatable, Sendable {
    public let storeID: String
    /// The one-time secret, standard base64.
    public let secret: String
    /// The keeper's signing key, standard base64.
    public let keeperKey: String
    /// The Google client id the phone signs in with.
    public let client: String?

    /// Reads the Drive fields of a `byobot://pair` link.
    public init(link: String) throws {
        var query: [String: String] = [:]
        for item in (try? PairingLink.items(link)) ?? [] where query[item.name] == nil { query[item.name] = item.value }
        guard let store = query["store_id"], !store.isEmpty, query["provider"] == "google",
              let secret = query["secret"].flatMap(PairingLink.data(fromBase64URL:)),
              let keeper = query["keeper_key"].flatMap(PairingLink.data(fromBase64URL:)), keeper.count == 32 else {
            throw CloudError("that isn't a byobot cloud join code this version understands")
        }
        storeID = store
        self.secret = secret.base64EncodedString()
        keeperKey = keeper.base64EncodedString()
        client = query["client"].flatMap { $0.isEmpty ? nil : $0 }
    }
}

/// A relay's epoch key as the phone keeps it in the keychain.
public struct EpochKey: Codable, Equatable, Sendable {
    public static let currentVersion = 1
    public var version = EpochKey.currentVersion
    public var epoch: Int
    public var key: Data

    public init(epoch: Int, key: Data) {
        self.epoch = epoch
        self.key = key
    }
}

/// Samples and deletions sent together, as in POST /v1/samples.
public struct SampleBatch: Codable, Equatable, Sendable {
    public var samples: [Sample]
    public var deleted: [Deletion]

    public init(samples: [Sample] = [], deleted: [Deletion] = []) {
        self.samples = samples
        self.deleted = deleted
    }

    var size: Int { samples.count + deleted.count }
}

public struct RejectedBatch: Codable, Equatable, Sendable {
    public var reason: String
    public var samples: SampleBatch
}

/// The keeper's word on one relay's segments in this epoch.
struct Receipt: Codable, Equatable, Sendable {
    struct Rejected: Codable, Equatable, Sendable {
        var seq: Int
        var line: Int
        var reason: String
    }

    var read: [Int]
    var rejected: [Rejected]?
}

/// One unread segment: the events and sample batches (by line) it carried.
struct Carried: Codable, Equatable, Sendable {
    var events: [String]
    var samples: [Int: SampleBatch]
}

public struct CloudRelay: Codable, Equatable, Sendable {
    /// The hub's batch limit, which a segment keeps to as well.
    public static let maxBatch = 2000
    /// Paths taken by a file this relay didn't know it wrote, skipped in a row before giving up.
    static let maxSkips = 3

    /// The file's format.
    public static let currentVersion = 1
    public private(set) var version = CloudRelay.currentVersion
    public let storeID: String
    public let deviceID: String
    /// The keeper key this relay trusts. An epoch file's handover moves it to the keeper's new key.
    public private(set) var keeperSign: String
    public private(set) var epoch: Int?
    /// The epoch's key. It is kept in the keychain, not with the rest: see `epochKey`.
    var key: Data?
    var seq = 0
    /// Sample batches not yet in a segment.
    public private(set) var samples: [SampleBatch] = []
    var sent: [String: Carried] = [:]
    /// Sample batches the keeper rejected, with why.
    public private(set) var rejected: [RejectedBatch] = []
    /// Deletions an older app held back; they go on the next sync.
    public private(set) var held: [Deletion] = []
    /// Events the keeper read since they were last taken.
    var delivered: [String] = []
    public private(set) var plan: PlanWindow?
    var receipts: [String: Receipt] = [:]
    var planPath: String?
    public private(set) var removed = false

    enum CodingKeys: String, CodingKey {
        case version, epoch, seq, samples, sent, rejected, held, delivered, plan, receipts, removed
        case storeID = "store_id"
        case deviceID = "device_id"
        case keeperSign = "keeper_sign"
        case planPath = "plan_path"
    }

    /// The epoch's key, to keep in the keychain.
    public var epochKey: EpochKey? {
        guard let epoch, let key else { return nil }
        return EpochKey(epoch: epoch, key: key)
    }

    /// Puts back the key kept for this relay's epoch. One for another epoch is left out; the relay reads its
    /// key file again.
    public mutating func restore(_ kept: EpochKey?) {
        guard let kept, kept.version <= EpochKey.currentVersion, kept.epoch == epoch else { return }
        key = kept.key
    }

    public struct Report: Equatable, Sendable {
        public var epoch: Int?
        public var written: Int
        public var pending: Int
        public var removed: Bool
    }

    init(storeID: String, deviceID: String, keeperSign: String) {
        self.storeID = storeID
        self.deviceID = deviceID
        self.keeperSign = keeperSign
    }

    /// Joins with a code the Mac showed: writes this device's file with its proof that it holds the code's secret.
    public static func join(_ folder: some CloudFolder, link: String, deviceID: String,
                            identity: CloudIdentity) async throws -> CloudRelay {
        let code = try CloudJoin(link: link)
        guard let secret = Data(base64Encoded: code.secret) else { throw CloudError("that isn't a byobot cloud join code") }
        let relay = CloudRelay(storeID: code.storeID, deviceID: deviceID, keeperSign: code.keeperKey)
        let proof = Cloud.joinProof(secret: secret, storeID: code.storeID, deviceID: deviceID, identity: identity)
        var signed = false
        for path in try await folder.list().keys.filter({ Self.epochNumber($0) != nil }).sorted(by: >) {
            if let n = Self.epochNumber(path), let data = try await folder.read(path),
               (try? Cloud.readEpoch(data, keeperSign: code.keeperKey, storeID: code.storeID, epoch: n)) != nil {
                signed = true
                break
            }
        }
        guard signed else {
            throw CloudError("This Google account doesn't hold that Mac's logbook. Sign in with the account the Mac uses.")
        }
        let file = try Cloud.deviceFile(storeID: code.storeID, id: deviceID, deviceClass: "relay", identity: identity,
                                        proof: proof)
        do {
            try await folder.create(Cloud.devicePath(deviceID), file)
        } catch let taken as CloudExists {
            // Left by a join the Mac never finished: answer this code instead. A phone with a key is already in.
            if try await folder.list().keys.contains(where: { $0.hasPrefix("keys/") && $0.hasSuffix("/\(deviceID).key") }) {
                throw taken
            }
            try await folder.delete(Cloud.devicePath(deviceID))
            try await folder.create(Cloud.devicePath(deviceID), file)
        }
        return relay
    }

    public var joined: Bool { key != nil }

    /// Every sample and deletion the keeper hasn't read: queued, in unread segments, or held. A phone leaving Drive
    /// sync sends these another way, since Apple Health won't hand them over again.
    public var leftovers: SampleBatch {
        let batches = samples + sent.keys.sorted().flatMap { path in
            sent[path].map { c in c.samples.keys.sorted().compactMap { c.samples[$0] } } ?? []
        }
        return SampleBatch(samples: batches.flatMap(\.samples), deleted: batches.flatMap(\.deleted) + held)
    }

    /// The events the keeper read since this was last asked.
    public mutating func takeDelivered() -> Set<String> {
        defer { delivered = [] }
        return Set(delivered)
    }

    /// Queues samples and deletions. A deletion of a sample not yet sent drops it; the Mac keeps a deletion that
    /// comes before its sample, so none waits.
    public mutating func send(_ batch: SampleBatch) {
        drop(Set(batch.deleted.map(\.sourceId)))
        queue(batch)
    }

    mutating func drop(_ gone: Set<String>) {
        guard !gone.isEmpty else { return }
        for i in samples.indices { samples[i].samples.removeAll { gone.contains($0.sourceId) } }
        samples.removeAll { $0.size == 0 }
    }

    mutating func release() {
        guard !held.isEmpty else { return }
        drop(Set(held.map(\.sourceId)))
        queue(SampleBatch(deleted: held))
        held = []
    }

    mutating func queue(_ batch: SampleBatch) {
        enum Item { case sample(Sample), deletion(Deletion) }
        let items = batch.samples.map(Item.sample) + batch.deleted.map(Item.deletion)
        for start in stride(from: 0, to: items.count, by: Self.maxBatch) {
            var part = SampleBatch()
            for item in items[start..<min(start + Self.maxBatch, items.count)] {
                switch item {
                case .sample(let s): part.samples.append(s)
                case .deletion(let d): part.deleted.append(d)
                }
            }
            samples.append(part)
        }
    }

    // MARK: sync

    /// Takes a newer epoch's key, reads the plan, then sends what the keeper doesn't hold of `pending`, the
    /// logbook's events still waiting. `save` is called before each segment is written, so a relay that dies
    /// mid-write never reuses its seq.
    public mutating func sync(_ folder: some CloudFolder, identity: CloudIdentity, pending: [Event] = [],
                              save: (CloudRelay) async throws -> Void = { _ in }) async throws -> Report {
        guard !removed else { return report(0, pending) }
        let listing = try await folder.list()
        let moved = try await adopt(folder, listing, identity)
        if removed {  // no keeper reads what it left in the folder now
            for path in listing.keys where path.hasPrefix("segments/\(deviceID)/") || path == Cloud.devicePath(deviceID) {
                try? await folder.delete(path)
            }
        }
        guard !removed, key != nil else { return report(0, pending) }
        try await readPlan(folder, listing)
        settle(listing)
        release()
        let written = try await send(folder, identity, pending, force: moved, save: save)
        return report(written, pending)
    }

    func report(_ written: Int, _ pending: [Event]) -> Report {
        let done = Set(delivered)
        return Report(epoch: epoch, written: written, pending: pending.filter { !done.contains($0.id) }.count,
                      removed: removed)
    }

    /// The epoch files newer than this relay's, oldest first, each checked against the keeper key the one before
    /// it trusts: a handover moves that trust to the keeper's new key.
    func follow(_ folder: some CloudFolder, _ listing: [String: String]) async throws -> [Int: [String: JSONValue]] {
        var bodies: [Int: [String: JSONValue]] = [:]
        var trusted = keeperSign
        for n in listing.keys.compactMap(Self.epochNumber).sorted() {
            if let epoch, n < epoch || (n == epoch && key != nil) { continue }
            let data = try await folder.read(Cloud.epochPath(n)) ?? Data()
            guard let body = try? Cloud.readEpoch(data, keeperSign: trusted, storeID: storeID, epoch: n),
                  let signer = Cloud.text(body, "keeper_key") else { continue }
            bodies[n] = body
            trusted = signer
        }
        return bodies
    }

    mutating func adopt(_ folder: some CloudFolder, _ listing: [String: String], _ me: CloudIdentity) async throws -> Bool {
        let epochs = listing.keys.compactMap(Self.epochNumber).sorted(by: >)
        let bodies = try await follow(folder, listing)
        for n in bodies.keys.sorted(by: >) {
            guard let body = bodies[n], let signer = Cloud.text(body, "keeper_key") else { continue }
            guard let keyData = try await folder.read(Cloud.keyPath(n, deviceID)) else {
                if epoch != nil, !Self.lists(body, deviceID) {
                    removed = true
                    key = nil
                }
                return false
            }
            guard let opened = try? Cloud.openKey(keyData, me: me, deviceID: deviceID, keeperSign: signer,
                                                  storeID: storeID, epoch: n) else { continue }
            let moved = epoch != nil && epoch != n
            if epoch != n {
                plan = nil
                planPath = nil
                receipts = [:]
            }
            epoch = n
            key = opened
            keeperSign = signer
            return moved
        }
        if epoch == nil {  // admitted mid-epoch: no epoch file lists it yet, its key file does
            for n in epochs {
                guard let keyData = try await folder.read(Cloud.keyPath(n, deviceID)),
                      let opened = try? Cloud.openKey(keyData, me: me, deviceID: deviceID, keeperSign: keeperSign,
                                                      storeID: storeID, epoch: n) else { continue }
                key = opened
                epoch = n
                return false
            }
        }
        return false
    }

    mutating func readPlan(_ folder: some CloudFolder, _ listing: [String: String]) async throws {
        guard let epoch, let key else { return }
        let prefix = "plan/\(Cloud.pad(epoch, Cloud.digitCount))-"
        for path in listing.keys.filter({ $0.hasPrefix(prefix) }).sorted(by: >) {
            if let planPath, path <= planPath { return }  // never step back to an older plan
            guard let data = try await folder.read(path),
                  let found = try? Cloud.readSegment(data, path: path, storeID: storeID, key: key,
                                                     signPublic: keeperSign) else { continue }
            // Receipts count even when the plan beside them is one this app can't read; it keeps its last plan.
            if let value = found.messages.first(where: { $0["plan"] != nil })?["plan"],
               let window = try? Payloads.decode(PlanWindow.self, value) {
                plan = window
            }
            planPath = path
            receipts = found.messages.first(where: { $0["receipts"] != nil })?["receipts"]
                .flatMap { try? Payloads.decode([String: Receipt].self, $0) } ?? [:]
            return
        }
    }

    /// Drops what a read segment carried; queues again the samples of one that left the folder unread.
    mutating func settle(_ listing: [String: String]) {
        let mine = receipts[deviceID]
        let read = Set(mine?.read ?? [])
        var why: [String: String] = [:]
        for r in mine?.rejected ?? [] { why["\(r.seq)/\(r.line)"] = r.reason }
        for path in sent.keys.sorted() {
            guard let carried = sent[path], let seq = Cloud.parseName(path)?.seq else { continue }
            if read.contains(seq) {
                delivered.append(contentsOf: carried.events)
                for line in carried.samples.keys.sorted() {
                    if let reason = why["\(seq)/\(line)"], let batch = carried.samples[line] {
                        rejected.append(RejectedBatch(reason: reason, samples: batch))
                    }
                }
                sent[path] = nil
            } else if listing[path] == nil {
                samples.insert(contentsOf: carried.samples.keys.sorted().compactMap { carried.samples[$0] }, at: 0)
                sent[path] = nil
            }
        }
    }

    mutating func send(_ folder: some CloudFolder, _ me: CloudIdentity, _ pending: [Event], force: Bool,
                       save: (CloudRelay) async throws -> Void) async throws -> Int {
        guard let epoch, let key else { return 0 }
        let skip = Set(sent.values.flatMap(\.events)).union(delivered)
        var todo = pending.filter { !skip.contains($0.id) }.sorted { ($0.hlc, $0.id) < ($1.hlc, $1.id) }
        var force = force, written = 0, skips = 0
        while !todo.isEmpty || !samples.isEmpty || force {
            let batch = Array(todo.prefix(Self.maxBatch))
            var messages: [JSONValue] = []
            if !batch.isEmpty {
                messages.append(.object(["events": try Payloads.encode(EventsRequest(events: batch, kindVersions: KindVersion.all))]))
            }
            var room = Self.maxBatch - batch.count
            var batches: [SampleBatch] = []
            while let next = samples.first, room >= next.size {
                room -= next.size
                batches.append(samples.removeFirst())
            }
            var lines: [Int: SampleBatch] = [:]
            for b in batches {
                lines[messages.count] = b
                messages.append(.object(["samples": try Payloads.encode(b)]))
            }
            if messages.isEmpty && !force { break }
            seq += 1
            let path = Cloud.segmentPath(kind: "segment", epoch: epoch, seq: seq, deviceID: deviceID)
            sent[path] = Carried(events: batch.map(\.id), samples: lines)
            do {
                let data = try Cloud.writeSegment(kind: "segment", storeID: storeID, deviceID: deviceID, epoch: epoch,
                                                  seq: seq, messages: messages, key: key, signer: me)
                try await save(self)
                try await folder.create(path, data)
            } catch let taken as CloudExists {
                // a file this relay didn't know it wrote: skip its seq and send what this one carried in the next
                sent[path] = nil
                samples.insert(contentsOf: batches, at: 0)
                skips += 1
                if skips >= Self.maxSkips { throw taken }
                continue
            } catch {
                sent[path] = nil
                samples.insert(contentsOf: batches, at: 0)  // its events are still pending; its samples go back in line
                throw error
            }
            todo.removeFirst(batch.count)
            written += 1
            force = false
            skips = 0
        }
        return written
    }

    // MARK: helpers

    static func epochNumber(_ path: String) -> Int? {
        guard path.hasPrefix("epochs/"), path.hasSuffix(".json") else { return nil }
        return Cloud.digits(String(path.dropFirst("epochs/".count).dropLast(".json".count)), Cloud.digitCount)
    }

    static func lists(_ epoch: [String: JSONValue], _ deviceID: String) -> Bool {
        guard case .array(let devices)? = epoch["devices"] else { return false }
        return devices.contains {
            if case .object(let d) = $0 { return d["device_id"] == .string(deviceID) } else { return false }
        }
    }

}

/// A folder in memory, for tests.
public actor MemoryCloudFolder: CloudFolder {
    public private(set) var files: [String: Data] = [:]

    public init() {}

    public func list() -> [String: String] {
        files.mapValues { SHA256.hash(data: $0).hex }
    }

    public func read(_ path: String) -> Data? { files[path] }

    public func create(_ path: String, _ data: Data) throws {
        guard files[path] == nil else { throw CloudExists(path) }
        files[path] = data
    }

    public func delete(_ path: String) { files[path] = nil }

    public func put(_ path: String, _ data: Data) { files[path] = data }
}
