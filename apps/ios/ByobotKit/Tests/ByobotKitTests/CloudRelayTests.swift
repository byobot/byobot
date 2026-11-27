#if canImport(CryptoKit) && canImport(Compression)
import Foundation
import XCTest
@testable import ByobotKit

/// The phone's relay against a keeper played from the cloud vectors' keys: epoch 1's files are the vectors' own,
/// and the plan segments with their receipts are written here with the keeper's signing key.
final class CloudRelayTests: XCTestCase {
    struct Keeper {
        let store: String, id: String, identity: CloudIdentity, epochFile: Data, keyFile: Data, epochKey: Data
        let relayID: String, relay: CloudIdentity, joinSecret: String, deviceFile: Data
        var seq = 1

        var link: String {
            func q(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? s }
            return "byobot://cloud-join?store=\(store)&secret=\(q(joinSecret))&keeper=\(q(identity.signPublic))"
        }

        /// Publishes a plan with receipts for the relay: the segments it read and the sample lines it refused.
        mutating func publish(_ folder: MemoryCloudFolder, read: [Int] = [], refused: [(Int, Int, String)] = [],
                              erased: [ErasedRecord]? = nil) async throws {
            seq += 1
            let window = PlanWindow(from: "2026-11-01", to: "2026-12-31", erased: erased)
            var mine: [String: JSONValue] = ["read": .array(read.map { .number(Double($0)) })]
            if !refused.isEmpty {
                mine["refused"] = .array(refused.map {
                    .object(["seq": .number(Double($0.0)), "line": .number(Double($0.1)), "reason": .string($0.2)])
                })
            }
            let messages: [JSONValue] = [.object(["plan": try CloudRelay.encode(window)]),
                                         .object(["receipts": .object([relayID: .object(mine)])])]
            let data = try Cloud.writeSegment(kind: "plan", storeID: store, deviceID: id, epoch: 1, seq: seq,
                                              messages: messages, key: epochKey, signer: identity)
            await folder.put(Cloud.segmentPath(kind: "plan", epoch: 1, seq: seq), data)
        }
    }

    func keeper() throws -> Keeper { try Self.keeper() }

    static func keeper() throws -> Keeper {
        guard case .object(let doc) = try Fixtures.json("schema/fixtures/v1/cloud.json"),
              case .object(let inputs)? = doc["inputs"],
              case .object(let k)? = inputs["keeper"], case .object(let kp)? = k["private"],
              case .object(let r)? = inputs["relay"], case .object(let rp)? = r["private"] else {
            throw XCTSkip("no cloud vectors")
        }
        func b(_ s: String?) -> Data { Data(base64Encoded: s ?? "") ?? Data() }
        let valid = try Fixtures.cases("schema/fixtures/v1/cloud.json", key: "valid")
        func file(_ name: String) -> Data { b(valid.first { $0.string("name") == name }?.string("data")) }
        return Keeper(store: inputs.string("store_id") ?? "", id: k.string("id") ?? "",
                      identity: try CloudIdentity(privateBox: b(kp.string("box")), privateSign: b(kp.string("sign"))),
                      epochFile: file("epoch file"), keyFile: file("epoch key sealed to the relay"),
                      epochKey: b(inputs.string("epoch_key")), relayID: r.string("id") ?? "",
                      relay: try CloudIdentity(privateBox: b(rp.string("box")), privateSign: b(rp.string("sign"))),
                      joinSecret: inputs.string("join_secret") ?? "", deviceFile: file("relay device file"))
    }

    /// A relay that joined and took epoch 1's key.
    func joined(_ k: Keeper, _ folder: MemoryCloudFolder) async throws -> CloudRelay {
        await folder.put(Cloud.epochPath(1), k.epochFile)
        var relay = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
        await folder.put(Cloud.keyPath(1, k.relayID), k.keyFile)
        let report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.epoch, 1)
        return relay
    }

    func event(_ n: Int) -> Event { Self.event(n) }

    static func event(_ n: Int) -> Event {
        Event(id: "0193a0b0-0000-7000-8000-0000000e\(String(format: "%04d", n))", schemaVersion: 1,
              record: RecordRef(kind: "effort", id: "0193a0b0-0000-7000-8000-0000000f\(String(format: "%04d", n))"),
              op: .put, kindVersion: 1, hlc: String(format: "%016d-0000", n), ts: "2026-11-23T08:00:00-05:00",
              deviceId: "0193a0b0-0000-7000-8000-00000000d002", actor: Actor(kind: .human, id: "me"), prev: [],
              payload: .object([:]))
    }

    func weighed(_ id: String) -> Sample { Self.weighed(id) }

    static func weighed(_ id: String) -> Sample {
        Sample(source: "apple_health", sourceId: id, type: "HKQuantityTypeIdentifierBodyMass",
               start: "2026-11-23T06:30:00-05:00", value: .number(80), unit: "kg")
    }

    func deletion(_ id: String) -> Deletion { Deletion(source: "apple_health", sourceId: id) }

    /// The messages in the relay's segment `seq`, as the keeper reads them.
    func segment(_ k: Keeper, _ folder: MemoryCloudFolder, _ seq: Int) async throws -> [[String: JSONValue]]? {
        let path = Cloud.segmentPath(kind: "segment", epoch: 1, seq: seq, deviceID: k.relayID)
        guard let data = await folder.files[path] else { return nil }
        return try Cloud.readSegment(data, path: path, storeID: k.store, key: k.epochKey,
                                     signPublic: k.relay.signPublic).messages
    }

    func sourceIds(_ messages: [[String: JSONValue]]?, _ field: String) throws -> [String] {
        let batches = try (messages ?? []).compactMap { $0["samples"] }.map { try CloudRelay.decode(SampleBatch.self, $0) }
        return batches.flatMap { field == "samples" ? $0.samples.map(\.sourceId) : $0.deleted.map(\.sourceId) }
    }

    // MARK: joining

    func testJoiningWritesTheDeviceFileTheKeeperChecks() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        await folder.put(Cloud.epochPath(1), k.epochFile)
        _ = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
        let written = await folder.files[Cloud.devicePath(k.relayID)]
        XCTAssertEqual(written, k.deviceFile)
    }

    func testJoiningAFolderTheMacDoesntKeepFails() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        do {
            _ = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
            XCTFail("joined another account's folder")
        } catch let error as CloudError {
            XCTAssertTrue(error.message.contains("Google account"))
        }
        let written = await folder.files[Cloud.devicePath(k.relayID)]
        XCTAssertNil(written)
    }

    func testAPhoneTheMacNeverLetInCanAnswerANewCode() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        await folder.put(Cloud.epochPath(1), k.epochFile)
        await folder.put(Cloud.devicePath(k.relayID), Data("left by an earlier code".utf8))
        _ = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
        let written = await folder.files[Cloud.devicePath(k.relayID)]
        XCTAssertEqual(written, k.deviceFile)
        await folder.put(Cloud.keyPath(1, k.relayID), k.keyFile)
        do {
            _ = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
            XCTFail("a phone already in joined again")
        } catch is CloudExists {}
    }

    func testWhatTheMacHasntReadIsLeftOverForAnotherRoute() async throws {
        var k = try keeper()
        let folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.send(SampleBatch(samples: [weighed("W1")]))
        _ = try await relay.sync(folder, identity: k.relay)  // in a segment the Mac hasn't read
        relay.send(SampleBatch(samples: [weighed("W2")], deleted: [deletion("W1")]))  // W2 and W1's deletion queued
        XCTAssertEqual(relay.leftovers.samples.map(\.sourceId).sorted(), ["W1", "W2"])
        XCTAssertEqual(relay.leftovers.deleted.map(\.sourceId), ["W1"])
        try await k.publish(folder, read: [relay.seq])
        _ = try await relay.sync(folder, identity: k.relay)
        XCTAssertFalse(relay.leftovers.samples.map(\.sourceId).contains("W1"))
    }

    func testAJoinCodeIsReadWithOrWithoutItsGoogleClient() throws {
        let code = try CloudJoin(link: "byobot://cloud-join?store=s&secret=a%2Bb%3D&keeper=k&client=ios.apps.example")
        XCTAssertEqual(code.secret, "a+b=")
        XCTAssertEqual(code.client, "ios.apps.example")
        XCTAssertNil(try CloudJoin(link: "byobot://cloud-join?store=s&secret=x&keeper=k").client)
        XCTAssertThrowsError(try CloudJoin(link: "https://cloud-join?store=s&secret=x&keeper=k"))
        XCTAssertThrowsError(try CloudJoin(link: "byobot://cloud-join?store=s&keeper=k"))
    }

    func testARelayWithoutAKeyYetWritesNothing() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        await folder.put(Cloud.epochPath(1), k.epochFile)
        var relay = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
        relay.log([event(1)])
        let report = try await relay.sync(folder, identity: k.relay)
        XCTAssertNil(report.epoch)
        XCTAssertEqual(report.written, 0)
        XCTAssertFalse(relay.joined)
    }

    // MARK: events

    func testEventsGoOnceAndLeaveWhenTheKeeperReadsThem() async throws {
        var k = try keeper()
        let folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.log([event(2), event(1)])
        var report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.written, 1)
        guard case .object(let body)? = try await segment(k, folder, 1)?.first?["events"],
              case .array(let events)? = body["events"] else { return XCTFail("no events message") }
        XCTAssertEqual(try events.map { try CloudRelay.decode(Event.self, $0).id }, [event(1).id, event(2).id])
        XCTAssertNotNil(body["kinds"])
        report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.written, 0)  // still unread, not sent again
        var delivered = relay.takeDelivered()
        XCTAssertEqual(delivered, [])
        try await k.publish(folder, read: [1])
        report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.pending, 0)
        delivered = relay.takeDelivered()
        XCTAssertEqual(delivered, [event(1).id, event(2).id])
        delivered = relay.takeDelivered()
        XCTAssertEqual(delivered, [])
    }

    func testEventsOfAnErasedRecordAreNotSent() async throws {
        var k = try keeper()
        let folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.log([event(1)])
        try await k.publish(folder, erased: [ErasedRecord(kind: "effort", id: event(1).record.id)])
        let report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.written, 0)
        XCTAssertEqual(report.pending, 0)
        XCTAssertNotNil(relay.plan)
    }

    func testANewerPlanReplacesTheOneBeforeIt() async throws {
        var k = try keeper()
        let folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        try await k.publish(folder)
        try await k.publish(folder, erased: [ErasedRecord(kind: "effort", id: "x")])
        _ = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(relay.plan?.erased?.map(\.id), ["x"])
        XCTAssertEqual(relay.planPath, Cloud.segmentPath(kind: "plan", epoch: 1, seq: 3))
    }

    // MARK: samples

    func testSamplesOfASegmentThatLeftUnreadGoAgain() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.send(SampleBatch(samples: [weighed("W1")]))
        _ = try await relay.sync(folder, identity: k.relay)
        await folder.delete(Cloud.segmentPath(kind: "segment", epoch: 1, seq: 1, deviceID: k.relayID))
        let report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.written, 1)
        let written = try await segment(k, folder, 2)
        XCTAssertEqual(try sourceIds(written, "samples"), ["W1"])
    }

    func testADeletionGoesAtOnceThoughTheKeeperHasntReadItsSample() async throws {
        var k = try keeper()
        let folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.send(SampleBatch(samples: [weighed("W1"), weighed("W2")]))
        _ = try await relay.sync(folder, identity: k.relay)
        relay.send(SampleBatch(deleted: [deletion("W1")]))
        XCTAssertEqual(relay.unread, ["W1", "W2"])
        let report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.written, 1)
        XCTAssertEqual(relay.held, [])
        let written = try await segment(k, folder, 2)
        XCTAssertEqual(try sourceIds(written, "deleted"), ["W1"])
    }

    func testASampleDeletedBeforeItWasSentNeverGoes() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.send(SampleBatch(samples: [weighed("W1"), weighed("W2")]))
        relay.send(SampleBatch(deleted: [deletion("W1")]))
        _ = try await relay.sync(folder, identity: k.relay)
        let sent = try await segment(k, folder, 1)
        XCTAssertEqual(try sourceIds(sent, "samples"), ["W2"])
        XCTAssertEqual(try sourceIds(sent, "deleted"), ["W1"])
    }

    func testRefusedSamplesAreKeptWithWhy() async throws {
        var k = try keeper()
        let folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.send(SampleBatch(samples: [weighed("W1")]))
        _ = try await relay.sync(folder, identity: k.relay)
        try await k.publish(folder, read: [1], refused: [(1, 0, "not a reading byobot knows")])
        _ = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(relay.refused.map(\.reason), ["not a reading byobot knows"])
        XCTAssertEqual(relay.unread, [])
    }

    func testABigQueueIsSplitAtTheBatchLimit() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.send(SampleBatch(samples: (0..<(CloudRelay.maxBatch + 5)).map { weighed("W\($0)") }))
        let report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.written, 2)
        let written = try await segment(k, folder, 2)
        XCTAssertEqual(try sourceIds(written, "samples").count, 5)
    }

    // MARK: failures

    /// A folder whose next uploads fail.
    actor Failing: CloudFolder {
        let inner: MemoryCloudFolder
        var failures: Int
        init(_ inner: MemoryCloudFolder, failures: Int) {
            self.inner = inner
            self.failures = failures
        }
        func list() async throws -> [String: String] { await inner.list() }
        func read(_ path: String) async throws -> Data? { await inner.read(path) }
        func create(_ path: String, _ data: Data) async throws {
            if failures > 0 {
                failures -= 1
                throw URLError(.timedOut)
            }
            try await inner.create(path, data)
        }
        func delete(_ path: String) async throws { await inner.delete(path) }
    }

    func testAFailedUploadKeepsItsSamplesForTheNextSync() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.log([event(1)])
        relay.send(SampleBatch(samples: [weighed("W1")]))
        do {
            _ = try await relay.sync(Failing(folder, failures: 1), identity: k.relay)
            XCTFail("the upload failed")
        } catch is URLError {}
        XCTAssertEqual(relay.unread, ["W1"])
        _ = try await relay.sync(folder, identity: k.relay)
        let sent = try await segment(k, folder, 2)
        XCTAssertEqual(try sourceIds(sent, "samples"), ["W1"])
        XCTAssertNotNil(sent?.first?["events"])
    }

    func testAPathTakenByAnUnknownFileIsSkipped() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        await folder.put(Cloud.segmentPath(kind: "segment", epoch: 1, seq: 1, deviceID: k.relayID), Data("x".utf8))
        relay.send(SampleBatch(samples: [weighed("W1")]))
        let report = try await relay.sync(folder, identity: k.relay)
        XCTAssertEqual(report.written, 1)
        let written = try await segment(k, folder, 2)
        XCTAssertEqual(try sourceIds(written, "samples"), ["W1"])
    }

    func testTheRelayIsSavedBeforeEachWriteAndReadsBackTheSame() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        var relay = try await joined(k, folder)
        relay.send(SampleBatch(samples: [weighed("W1")]))
        var saved: [CloudRelay] = []
        _ = try await relay.sync(folder, identity: k.relay) { saved.append($0) }
        XCTAssertEqual(saved.map(\.seq), [1])
        XCTAssertEqual(saved.first?.inFlight, ["W1"])
        let data = try JSONEncoder().encode(relay)
        var back = try JSONDecoder().decode(CloudRelay.self, from: data)
        XCTAssertFalse(back.joined)
        back.restore(relay.epochKey)
        XCTAssertEqual(back, relay)
    }

    func testTheRelaysFileHasSnakeCaseKeysAndNoEpochKey() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        let relay = try await joined(k, folder)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(relay)) as? [String: Any])
        XCTAssertEqual(object["version"] as? Int, CloudRelay.currentVersion)
        XCTAssertEqual(object["store_id"] as? String, k.store)
        XCTAssertNotNil(object["device_id"])
        XCTAssertNotNil(object["keeper_sign"])
        XCTAssertNil(object["key"])
        XCTAssertNil(object["storeID"])
    }

    func testAKeyForAnotherEpochIsNotRestored() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        let relay = try await joined(k, folder)
        let kept = try XCTUnwrap(relay.epochKey)
        var back = try JSONDecoder().decode(CloudRelay.self, from: JSONEncoder().encode(relay))
        back.restore(EpochKey(epoch: kept.epoch + 1, key: kept.key))
        XCTAssertFalse(back.joined)
        back.restore(nil)
        XCTAssertFalse(back.joined)
    }

    func testARelayThatLostItsKeyReadsItAgainWithoutMoving() async throws {
        let k = try keeper(), folder = MemoryCloudFolder()
        let relay = try await joined(k, folder)
        var back = try JSONDecoder().decode(CloudRelay.self, from: JSONEncoder().encode(relay))
        back.send(SampleBatch(samples: [weighed("W1")]))
        let report = try await back.sync(folder, identity: k.relay)
        XCTAssertEqual(report.epoch, 1)
        XCTAssertTrue(back.joined)
        XCTAssertEqual(report.written, 1)
        XCTAssertEqual(back.epochKey, relay.epochKey)
    }
}
#endif
