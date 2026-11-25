import Foundation
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
                continue
            }
            guard case .object(let expect)? = item["expect"], case .array(let hosts)? = expect["hosts"],
                  case .number(let port)? = expect["port"] else { return XCTFail("\(name): bad fixture") }
            let parsed = try PairingLink(link: link)
            XCTAssertEqual(parsed.hosts.map { JSONValue.string($0) }, hosts, name)
            XCTAssertEqual(parsed.port, Int(port), name)
            XCTAssertEqual(parsed.code, expect.string("code"), name)
            XCTAssertEqual(parsed.fingerprint, expect.string("fp"), name)
        }
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
