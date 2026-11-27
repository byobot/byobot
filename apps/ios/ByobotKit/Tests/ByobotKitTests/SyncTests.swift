import Foundation
import Security
import XCTest
@testable import ByobotKit

/// The sync protocol fixtures (schema/fixtures/v1/sync.json), shared with `byobot serve`'s tests.
final class SyncTests: XCTestCase {
    func decode<T: Codable>(_ type: T.Type, _ value: JSONValue, roundTrip: Bool, _ name: String) throws {
        let decoded: T
        do {
            decoded = try JSONDecoder().decode(T.self, from: Fixtures.data(value))
        } catch {
            return XCTFail("\(name): does not decode as \(T.self): \(error)")
        }
        if roundTrip {
            XCTAssertEqual(try JSONValue.parse(JSONEncoder().encode(decoded)), value, "\(name): does not round-trip")
        }
    }

    func testEveryValidSyncMessageDecodes() throws {
        var checked = 0
        for item in try Fixtures.cases("schema/fixtures/v1/sync.json") where item.bool("valid") == true {
            let name = item.string("name") ?? "?"
            let data = try XCTUnwrap(item["data"])
            switch item.string("def") {
            // What the app sends must come out byte-for-byte equivalent.
            case "pair_request": try decode(PairRequest.self, data, roundTrip: true, name)
            case "events_request": try decode(EventsRequest.self, data, roundTrip: true, name)
            case "samples_request": try decode(SamplesRequest.self, data, roundTrip: true, name)
            // What it receives only has to be read.
            case "hello": try decode(SyncHello.self, data, roundTrip: false, name)
            case "pair_response": try decode(PairResponse.self, data, roundTrip: false, name)
            case "status": try decode(SyncStatus.self, data, roundTrip: false, name)
            case "plan_window": try decode(PlanWindow.self, data, roundTrip: true, name)
            case "events_response": try decode(EventsResponse.self, data, roundTrip: false, name)
            case "pull_response": try decode(PullResponse.self, data, roundTrip: true, name)
            case "samples_response": try decode(SamplesResponse.self, data, roundTrip: false, name)
            case "error": try decode(SyncError.self, data, roundTrip: false, name)
            default: XCTFail("\(name): no Swift type for \(item.string("def") ?? "?")")
            }
            checked += 1
        }
        XCTAssertGreaterThanOrEqual(checked, 11)
    }

    func testPairingLinks() throws {
        for item in try Fixtures.cases("schema/fixtures/v1/sync.json", key: "links") {
            let name = item.string("name") ?? "?"
            let link = try XCTUnwrap(item.string("link"))
            if item.bool("valid") == false {
                XCTAssertThrowsError(try PairingLink(link: link), name)
                XCTAssertThrowsError(try CloudJoin(link: link), name)
                continue
            }
            guard case .object(let expect)? = item["expect"] else { return XCTFail("\(name): bad fixture") }
            if case .array(let hosts)? = expect["hosts"], case .number(let port)? = expect["port"] {
                let parsed = try PairingLink(link: link)
                XCTAssertEqual(parsed.hosts.map { JSONValue.string($0) }, hosts, name)
                XCTAssertEqual(parsed.port, Int(port), name)
                XCTAssertEqual(parsed.code, expect.string("code"), name)
                XCTAssertEqual(parsed.fingerprint, expect.string("pin"), name)
            } else {
                XCTAssertThrowsError(try PairingLink(link: link), name)
            }
            if let store = expect.string("store_id") {
                let join = try CloudJoin(link: link)
                XCTAssertEqual(join.storeID, store, name)
                XCTAssertEqual(join.secret, expect.string("secret"), name)
                XCTAssertEqual(join.keeperKey, expect.string("keeper_key"), name)
                XCTAssertEqual(join.client, expect.string("client"), name)
            } else {
                XCTAssertThrowsError(try CloudJoin(link: link), name)
            }
        }
    }

    func testThePinIsTheSHA256OfThePublicKey() throws {
        guard case .object(let doc) = try Fixtures.json("schema/fixtures/v1/sync.json"),
              case .object(let pin)? = doc["pin"], let der = pin.string("certificate").flatMap({ Data(base64Encoded: $0) }),
              let certificate = SecCertificateCreateWithData(nil, der as CFData) else { return XCTFail("bad fixture") }
        XCTAssertEqual(Pinner.fingerprint(certificate), pin.string("pin"))
    }

    func testLinksFromOutsideTheAppKeepOnlyLocalHosts() {
        for host in ["laptop.local", "Laptop.LOCAL.", "10.0.0.2", "172.16.4.1", "172.31.255.255", "192.168.1.20",
                     "169.254.3.4", "127.0.0.1", "fe80::1", "[fe80::1%en0]", "fd12:3456::1", "::1"] {
            XCTAssertTrue(PairingLink.isLocal(host), host)
        }
        for host in ["example.com", "local", ".local", "laptop.local.example.com", "8.8.8.8", "172.32.0.1",
                     "192.169.0.1", "100.64.0.1", "2001:db8::1", "fe80", "10.0.0", "10.0.0.256"] {
            XCTAssertFalse(PairingLink.isLocal(host), host)
        }
        let fp = String(repeating: "ab", count: 32)
        let mixed = PairingLink(hosts: ["hub.example.com", "laptop.local"], port: 8765, code: "123456", fingerprint: fp)
        XCTAssertEqual(mixed.localOnly?.hosts, ["laptop.local"])
        XCTAssertNil(PairingLink(hosts: ["hub.example.com"], port: 8765, code: "123456", fingerprint: fp).localOnly)
    }

    func testPairingSaysWhenItReplacesAnotherLaptop() {
        let link = PairingLink(hosts: ["laptop.local"], port: 8765, code: "123456", fingerprint: String(repeating: "ab", count: 32))
        func paired(_ fp: String) -> PairedServer {
            PairedServer(hosts: ["laptop.local"], port: 8765, fingerprint: fp, deviceId: "dev_1", name: "Laptop",
                         pairedAt: "2026-01-01T00:00:00Z")
        }
        XCTAssertFalse(link.replaces(nil))
        XCTAssertFalse(link.replaces(paired(link.fingerprint)))
        XCTAssertTrue(link.replaces(paired(String(repeating: "cd", count: 32))))
    }

    func testShortFingerprintMatchesWhatTheLaptopPrints() {
        XCTAssertEqual(PairingLink.shortFingerprint("48e95ac971b683d1e51e66dcea164007"), "48e9 5ac9 71b6 83d1")
    }

    func testURLsBracketIPv6() {
        XCTAssertEqual(SyncClient.url(host: "fe80::1", port: 8765, path: "/v1/hello")?.absoluteString,
                       "https://[fe80::1]:8765/v1/hello")
        XCTAssertEqual(SyncClient.url(host: "laptop.local", port: 8765, path: "/v1/plan?from=2026-11-22")?.absoluteString,
                       "https://laptop.local:8765/v1/plan?from=2026-11-22")
    }

    func testServerErrorsBecomeClientErrors() throws {
        let url = URL(string: "https://laptop.local:8765/v1/status")!
        let unauthorized = HTTPURLResponse(url: url, statusCode: 401, httpVersion: nil, headerFields: nil)!
        XCTAssertThrowsError(try SyncClient.decode(data: Data(), response: unauthorized) as SyncStatus) {
            XCTAssertEqual($0 as? SyncClientError, .unauthorized)
        }
        let forbidden = HTTPURLResponse(url: url, statusCode: 403, httpVersion: nil, headerFields: nil)!
        let body = Data(#"{"error": "wrong_code", "message": "that code is not right", "details": {"tries_left": 4}}"#.utf8)
        XCTAssertThrowsError(try SyncClient.decode(data: body, response: forbidden) as PairResponse) {
            XCTAssertEqual($0 as? SyncClientError, .server(status: 403, error: "wrong_code", message: "that code is not right"))
        }
    }
}
