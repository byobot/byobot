import Foundation
import XCTest
@testable import ByobotKit

/// Cloud protocol 1's golden vectors (schema/fixtures/v1/cloud.json), the same files the Python tests open.
final class CloudTests: XCTestCase {
    struct Vectors {
        let inputs: [String: JSONValue]
        let valid: [[String: JSONValue]]
        let invalid: [[String: JSONValue]]

        func who(_ name: String) -> [String: JSONValue] {
            if case .object(let o)? = inputs[name] { return o }
            return [:]
        }

        var store: String { inputs.string("store_id") ?? "" }
    }

    func vectors() throws -> Vectors {
        guard case .object(let doc) = try Fixtures.json("schema/fixtures/v1/cloud.json"),
              case .object(let inputs)? = doc["inputs"] else {
            throw XCTSkip("no cloud vectors")
        }
        return Vectors(inputs: inputs, valid: try Fixtures.cases("schema/fixtures/v1/cloud.json", key: "valid"),
                       invalid: try Fixtures.cases("schema/fixtures/v1/cloud.json", key: "invalid"))
    }

    func bytes(_ text: String?) -> Data { Data(base64Encoded: text ?? "") ?? Data() }

    func relayIdentity(_ v: Vectors) throws -> CloudIdentity {
        guard case .object(let p)? = v.who("relay")["private"] else { throw XCTSkip("no relay keys") }
        return try CloudIdentity(privateBox: bytes(p.string("box")), privateSign: bytes(p.string("sign")))
    }

    func openFile(_ v: Vectors, _ path: String, _ data: Data, _ testCase: [String: JSONValue] = [:]) throws -> JSONValue {
        let store = testCase.string("store_id") ?? v.store
        let keeper = v.who("keeper"), relay = v.who("relay")
        if path.hasSuffix(".seg") {
            let relayID = relay.string("id") ?? ""
            let writer = path.hasPrefix("segments/") && path.contains(relayID) ? relay : keeper
            let found = try Cloud.readSegment(data, path: path, storeID: store,
                                              key: bytes(testCase.string("key") ?? v.inputs.string("epoch_key")),
                                              signPublic: testCase.string("signer") ?? writer.string("sign") ?? "")
            return .object(["header": .object(found.header), "messages": .array(found.messages.map { .object($0) })])
        }
        if path.hasPrefix("devices/") {
            let id = String(path.dropFirst("devices/".count).dropLast(".json".count))
            return .object(try Cloud.readDevice(data, storeID: store, deviceID: id))
        }
        let signer = testCase.string("signer") ?? keeper.string("sign") ?? ""
        if path.hasPrefix("epochs/") {
            let epoch = Int(path.dropFirst(7).prefix(4)) ?? -1
            return .object(try Cloud.readEpoch(data, keeperSign: signer, storeID: store, epoch: epoch))
        }
        let epoch = Int(path.dropFirst(5).prefix(4)) ?? -1
        if path.hasSuffix("recovery.key") {
            return .object(try Cloud.openRecovery(data, recoveryKey: bytes(v.inputs.string("recovery_key")),
                                                  storeID: store, epoch: epoch))
        }
        let key = try Cloud.openKey(data, me: relayIdentity(v), deviceID: relay.string("id") ?? "",
                                    keeperSign: signer, storeID: store, epoch: epoch)
        return .object(["epoch_key": .string(key.base64EncodedString())])
    }

    func testEachValidFileOpensToWhatItShould() throws {
        let v = try vectors()
        XCTAssertGreaterThanOrEqual(v.valid.count, 7)
        for c in v.valid {
            let name = c.string("name") ?? "?"
            XCTAssertEqual(try openFile(v, c.string("path") ?? "", bytes(c.string("data"))), c["expect"], name)
        }
    }

    func testEachBrokenFileIsRefused() throws {
        let v = try vectors()
        XCTAssertGreaterThanOrEqual(v.invalid.count, 10)
        for c in v.invalid {
            let name = c.string("name") ?? "?", wanted = c.string("error") ?? "?"
            do {
                _ = try openFile(v, c.string("path") ?? "", bytes(c.string("data")), c)
                XCTFail("\(name): was opened")
            } catch let error as CloudError {
                XCTAssertTrue(error.message.contains(wanted), "\(name): \(error.message)")
            }
        }
    }

    func testTheInputsMatchTheirPublicKeys() throws {
        let v = try vectors()
        let relay = try relayIdentity(v)
        XCTAssertEqual(relay.boxPublic, v.who("relay").string("box"))
        XCTAssertEqual(relay.signPublic, v.who("relay").string("sign"))
    }

    func testTheRelayDeviceFileMatchesByteForByte() throws {
        let v = try vectors()
        let relay = try relayIdentity(v), id = v.who("relay").string("id") ?? ""
        let proof = Cloud.joinProof(secret: bytes(v.inputs.string("join_secret")), storeID: v.store, deviceID: id,
                                    identity: relay)
        let file = try Cloud.deviceFile(storeID: v.store, id: id, deviceClass: "relay", identity: relay, proof: proof)
        let wanted = v.valid.first { $0.string("name") == "relay device file" }?.string("data")
        XCTAssertEqual(file.base64EncodedString(), wanted)
    }

    func testASegmentThePhoneWritesReadsBack() throws {
        let me = CloudIdentity(), key = Data((0..<32).map { UInt8($0) })
        let messages: [JSONValue] = [
            .object(["events": .object(["events": .array([])])]),
            .object(["samples": .object(["samples": .array([]), "deleted": .array([])])]),
        ]
        let store = "0193a0b0-0000-7000-8000-00000000c001"
        let data = try Cloud.writeSegment(kind: "segment", storeID: store, deviceID: "dev-1", epoch: 3, seq: 42,
                                          messages: messages, key: key, signer: me)
        let path = Cloud.segmentPath(kind: "segment", epoch: 3, seq: 42, deviceID: "dev-1")
        XCTAssertEqual(path, "segments/dev-1/0003-0000000042.seg")
        let found = try Cloud.readSegment(data, path: path, storeID: store, key: key, signPublic: me.signPublic)
        XCTAssertEqual(found.messages.map { JSONValue.object($0) }, messages)
        XCTAssertEqual(Cloud.parseName(path), SegmentName(kind: "segment", epoch: 3, seq: 42, deviceID: "dev-1"))
        XCTAssertNil(Cloud.parseName("segments/dev-1/3-42.seg"))
        XCTAssertThrowsError(try Cloud.readSegment(data, path: Cloud.segmentPath(kind: "segment", epoch: 3, seq: 43,
                                                                                deviceID: "dev-1"),
                                                   storeID: store, key: key, signPublic: me.signPublic))
    }

    func testAMessageANewerByobotAddedIsLeftOut() throws {
        let me = CloudIdentity(), key = Data((0..<32).map { UInt8($0) })
        let plan: JSONValue = .object(["receipts": .object([:])])
        let store = "0193a0b0-0000-7000-8000-00000000c001"
        let data = try Cloud.writeSegment(kind: "plan", storeID: store, deviceID: "dev-1", epoch: 3, seq: 7,
                                          messages: [plan, .object(["reminders": .object([:])])], key: key,
                                          signer: me)
        let path = Cloud.segmentPath(kind: "plan", epoch: 3, seq: 7, deviceID: nil)
        let found = try Cloud.readSegment(data, path: path, storeID: store, key: key, signPublic: me.signPublic)
        XCTAssertEqual(found.messages.map { JSONValue.object($0) }, [plan])
    }

    func testGzipRoundTripsAndRefusesOneCutShort() throws {
        let text = Data(String(repeating: "{\"end\":{\"lines\":0}}\n", count: 500).utf8)
        let packed = try Cloud.gzip(text)
        XCTAssertEqual(try Cloud.gunzip(packed), text)
        XCTAssertThrowsError(try Cloud.gunzip(packed.dropLast(20)))
    }
}
