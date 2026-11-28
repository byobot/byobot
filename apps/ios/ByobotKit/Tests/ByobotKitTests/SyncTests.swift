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
                continue
            }
            guard case .object(let expect)? = item["expect"] else { return XCTFail("\(name): bad fixture") }
            guard case .array(let hosts)? = expect["hosts"], case .number(let port)? = expect["port"] else {
                return XCTFail("\(name): bad fixture")
            }
            let parsed = try PairingLink(link: link)
            XCTAssertEqual(parsed.hosts.map { JSONValue.string($0) }, hosts, name)
            XCTAssertEqual(parsed.port, Int(port), name)
            XCTAssertEqual(parsed.code, expect.string("code"), name)
            XCTAssertEqual(parsed.fingerprint, expect.string("pin"), name)
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

    /// The hub's answers in sync.json's `exchanges`, which `byobot serve`'s tests get from a real hub: each request
    /// the hub takes decodes as the app's request type, and each error becomes the client error the app shows.
    func testTheSharedExchanges() throws {
        let exchanges = try Fixtures.cases("schema/fixtures/v1/sync.json", key: "exchanges")
        XCTAssertGreaterThanOrEqual(exchanges.count, 10)
        for x in exchanges {
            let name = x.string("name") ?? "?"
            let path = try XCTUnwrap(x.string("path"), name)
            let url = try XCTUnwrap(SyncClient.url(host: "laptop.local", port: 8765, path: path), name)
            XCTAssertEqual(url.absoluteString, "https://laptop.local:8765" + path, name)
            guard case .number(let code)? = x["status"] else { return XCTFail("\(name): bad fixture") }
            let status = Int(code)
            if status == 200, x.string("method") == "POST", let body = x["body"] {
                switch path {
                case "/v1/pair": try decode(PairRequest.self, body, roundTrip: false, name)
                case "/v1/events": try decode(EventsRequest.self, body, roundTrip: false, name)
                case "/v1/samples": try decode(SamplesRequest.self, body, roundTrip: false, name)
                default: XCTFail("\(name): no request type for \(path)")
                }
            }
            guard let error = x.string("error") else { continue }
            let response = try XCTUnwrap(HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil))
            let body = Data(#"{"error": "\#(error)", "message": "m"}"#.utf8)
            let want: SyncClientError = status == 401 ? .unauthorized : .server(status: status, error: error, message: "m")
            XCTAssertThrowsError(try SyncClient.decode(data: body, response: response) as SyncStatus, name) {
                XCTAssertEqual($0 as? SyncClientError, want, name)
            }
        }
    }
}
