// The phone as a relay in cloud protocol 1: it sends what it logged through the cloud folder and reads the keeper's
// plan. It follows python/src/byobot/cloud/relay.py.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

#if canImport(CryptoKit) && canImport(Compression)
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

/// What a `byobot://cloud-join` code holds.
public struct CloudJoin: Equatable, Sendable {
    public let storeID: String
    public let secret: String
    public let keeper: String
    /// The Google client id the phone signs in with.
    public let client: String?

    public init(link: String) throws {
        guard let parts = URLComponents(string: link), parts.scheme == "byobot", parts.host == "cloud-join" else {
            throw CloudError("that isn't a byobot cloud join code")
        }
        var query: [String: String] = [:]
        for item in parts.queryItems ?? [] where query[item.name] == nil { query[item.name] = item.value }
        guard let store = query["store"], let secret = query["secret"], let keeper = query["keeper"] else {
            throw CloudError("that isn't a byobot cloud join code")
        }
        storeID = store
        self.secret = secret
        self.keeper = keeper
        client = query["client"].flatMap { $0.isEmpty ? nil : $0 }
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

public struct RefusedBatch: Codable, Equatable, Sendable {
    public var reason: String
    public var samples: SampleBatch
}

/// The keeper's word on one relay's segments in this epoch.
struct Receipt: Codable, Equatable, Sendable {
    struct Refused: Codable, Equatable, Sendable {
        var seq: Int
        var line: Int
        var reason: String
    }

    var read: [Int]
    var refused: [Refused]?
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

    public let storeID: String
    public let deviceID: String
    public let keeperSign: String
    public private(set) var epoch: Int?
    var key: Data?
    var seq = 0
    /// Every event it was given and the keeper hasn't read yet.
    public private(set) var events: [String: Event] = [:]
    /// Sample batches not yet in a segment.
    public private(set) var samples: [SampleBatch] = []
    var sent: [String: Carried] = [:]
    /// Sample batches the keeper refused, with why.
    public private(set) var refused: [RefusedBatch] = []
    /// Deletions an older app held back; they go on the next sync.
    public private(set) var held: [Deletion] = []
    /// Events the keeper read since they were last taken.
    var delivered: [String] = []
    public private(set) var plan: PlanWindow?
    var receipts: [String: Receipt] = [:]
    var planPath: String?
    public private(set) var removed = false

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
        let relay = CloudRelay(storeID: code.storeID, deviceID: deviceID, keeperSign: code.keeper)
        let proof = Cloud.joinProof(secret: secret, storeID: code.storeID, deviceID: deviceID, identity: identity)
        var signed = false
        for path in try await folder.list().keys.filter({ Self.epochNumber($0) != nil }).sorted(by: >) {
            if let n = Self.epochNumber(path), let data = try await folder.read(path),
               (try? Cloud.readEpoch(data, keeperSign: code.keeper, storeID: code.storeID, epoch: n)) != nil {
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

    public mutating func log(_ events: [Event]) {
        for event in events { self.events[event.id] = event }
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

    /// The source ids of samples in segments the keeper hasn't read yet.
    public var inFlight: Set<String> {
        Set(sent.values.flatMap { $0.samples.values.flatMap { $0.samples.map(\.sourceId) } })
    }

    /// The source ids of samples the keeper hasn't read yet, queued or sent. A phone with another route sends a
    /// deletion of one of these here, not there.
    public var unread: Set<String> {
        inFlight.union(samples.flatMap { $0.samples.map(\.sourceId) })
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

    /// Takes a newer epoch's key, reads the plan, then sends what the keeper doesn't hold. `save` is called before
    /// each segment is written, so a relay that dies mid-write never reuses its seq.
    public mutating func sync(_ folder: some CloudFolder, identity: CloudIdentity,
                              save: (CloudRelay) async throws -> Void = { _ in }) async throws -> Report {
        guard !removed else { return Report(epoch: epoch, written: 0, pending: events.count, removed: true) }
        let listing = try await folder.list()
        let moved = try await adopt(folder, listing, identity)
        guard !removed, key != nil else {
            return Report(epoch: epoch, written: 0, pending: events.count, removed: removed)
        }
        try await readPlan(folder, listing)
        if let plan { applyPlan(plan) }
        settle(listing)
        release()
        let written = try await send(folder, identity, force: moved, save: save)
        return Report(epoch: epoch, written: written, pending: events.count, removed: false)
    }

    mutating func adopt(_ folder: some CloudFolder, _ listing: [String: String], _ me: CloudIdentity) async throws -> Bool {
        let epochs = listing.keys.compactMap(Self.epochNumber).sorted(by: >)
        for n in epochs {
            if let epoch, n <= epoch { break }
            let body: [String: JSONValue]
            do {
                body = try Cloud.readEpoch(try await folder.read(Cloud.epochPath(n)) ?? Data(), keeperSign: keeperSign,
                                           storeID: storeID, epoch: n)
            } catch is CloudError {
                continue
            }
            guard let keyData = try await folder.read(Cloud.keyPath(n, deviceID)) else {
                if epoch != nil, !Self.lists(body, deviceID) {
                    removed = true
                    key = nil
                }
                return false
            }
            guard let opened = try? Cloud.openKey(keyData, me: me, deviceID: deviceID, keeperSign: keeperSign,
                                                  storeID: storeID, epoch: n) else { continue }
            let first = epoch == nil
            epoch = n
            key = opened
            plan = nil
            planPath = nil
            receipts = [:]
            return !first
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
        let prefix = "plan/\(String(format: "%04d", epoch))-"
        for path in listing.keys.filter({ $0.hasPrefix(prefix) }).sorted(by: >) {
            if let planPath, path <= planPath { return }  // never step back to an older plan
            guard let data = try await folder.read(path),
                  let found = try? Cloud.readSegment(data, path: path, storeID: storeID, key: key,
                                                     signPublic: keeperSign),
                  let value = found.messages.first(where: { $0["plan"] != nil })?["plan"],
                  let window = try? Self.decode(PlanWindow.self, value) else { continue }
            plan = window
            planPath = path
            receipts = found.messages.first(where: { $0["receipts"] != nil })?["receipts"]
                .flatMap { try? Self.decode([String: Receipt].self, $0) } ?? [:]
            return
        }
    }

    mutating func applyPlan(_ plan: PlanWindow) {
        let erased = Set((plan.erased ?? []).map { RecordRef(kind: $0.kind, id: $0.id) })
        guard !erased.isEmpty else { return }
        events = events.filter { !erased.contains($0.value.record) }
    }

    /// Drops what a read segment carried; queues again the samples of one that left the folder unread.
    mutating func settle(_ listing: [String: String]) {
        let mine = receipts[deviceID]
        let read = Set(mine?.read ?? [])
        var why: [String: String] = [:]
        for r in mine?.refused ?? [] { why["\(r.seq)/\(r.line)"] = r.reason }
        for path in sent.keys.sorted() {
            guard let carried = sent[path], let seq = Cloud.parseName(path)?.seq else { continue }
            if read.contains(seq) {
                for id in carried.events where events.removeValue(forKey: id) != nil { delivered.append(id) }
                for line in carried.samples.keys.sorted() {
                    if let reason = why["\(seq)/\(line)"], let batch = carried.samples[line] {
                        refused.append(RefusedBatch(reason: reason, samples: batch))
                    }
                }
                sent[path] = nil
            } else if listing[path] == nil {
                samples.insert(contentsOf: carried.samples.keys.sorted().compactMap { carried.samples[$0] }, at: 0)
                sent[path] = nil
            }
        }
    }

    mutating func send(_ folder: some CloudFolder, _ me: CloudIdentity, force: Bool,
                       save: (CloudRelay) async throws -> Void) async throws -> Int {
        guard let epoch, let key else { return 0 }
        let inFolder = Set(sent.values.flatMap(\.events))
        var todo = events.values.filter { !inFolder.contains($0.id) }.sorted { ($0.hlc, $0.id) < ($1.hlc, $1.id) }
        var force = force, written = 0, skips = 0
        while !todo.isEmpty || !samples.isEmpty || force {
            let batch = Array(todo.prefix(Self.maxBatch))
            var messages: [JSONValue] = []
            if !batch.isEmpty {
                messages.append(.object(["events": try Self.encode(EventsRequest(events: batch, kinds: KindVersion.all))]))
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
                messages.append(.object(["samples": try Self.encode(b)]))
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
                samples.insert(contentsOf: batches, at: 0)  // its events are still in `events`; its samples go back in line
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
        guard path.hasPrefix("epochs/"), path.hasSuffix(".json"), path.count == 16 else { return nil }
        let digits = path.dropFirst(7).prefix(4)
        guard digits.allSatisfy(\.isASCII), digits.allSatisfy(\.isNumber) else { return nil }
        return Int(digits)
    }

    static func lists(_ epoch: [String: JSONValue], _ deviceID: String) -> Bool {
        guard case .array(let devices)? = epoch["devices"] else { return false }
        return devices.contains { if case .object(let d) = $0 { return d["id"] == .string(deviceID) } else { return false } }
    }

    static func encode<T: Encodable>(_ value: T) throws -> JSONValue {
        try JSONValue.parse(try JSONEncoder().encode(value))
    }

    static func decode<T: Decodable>(_ type: T.Type, _ value: JSONValue) throws -> T {
        try JSONDecoder().decode(type, from: try JSONEncoder().encode(value))
    }
}

/// A folder in memory, for tests.
public actor MemoryCloudFolder: CloudFolder {
    public private(set) var files: [String: Data] = [:]

    public init() {}

    public func list() -> [String: String] {
        files.mapValues { SHA256.hash(data: $0).map { String(format: "%02x", $0) }.joined() }
    }

    public func read(_ path: String) -> Data? { files[path] }

    public func create(_ path: String, _ data: Data) throws {
        guard files[path] == nil else { throw CloudExists(path) }
        files[path] = data
    }

    public func delete(_ path: String) { files[path] = nil }

    public func put(_ path: String, _ data: Data) { files[path] = data }
}
#endif
