import Foundation
import XCTest
@testable import ByobotKit

/// The generated types against the shared contract fixtures: every valid document decodes and
/// encodes back to the same JSON, so no field is dropped, renamed or reshaped on the way through.
final class ContractTests: XCTestCase {
    func roundTrip<T: Codable>(_ type: T.Type, _ value: JSONValue, _ name: String) throws {
        let decoded: T
        do {
            decoded = try JSONDecoder().decode(T.self, from: Fixtures.data(value))
        } catch {
            return XCTFail("\(name): does not decode as \(T.self): \(error)")
        }
        let again = try JSONValue.parse(JSONEncoder().encode(decoded))
        XCTAssertEqual(again, value, "\(name): \(T.self) does not encode back to the same JSON")
    }

    func testEveryValidEventAndSampleRoundTrips() throws {
        var checked = 0
        for item in try Fixtures.cases("schema/fixtures/v0/cases.json") where item.bool("valid") == true {
            let name = item.string("name") ?? "?"
            guard let data = item["data"] else { continue }
            switch item.string("schema") {
            case "event": try roundTrip(Event.self, data, name)
            case "telemetry": try roundTrip(TelemetrySample.self, data, name)
            default: XCTFail("\(name): unknown schema")
            }
            checked += 1
        }
        XCTAssertGreaterThan(checked, 20)
    }

    func testEventPayloadFollowsItsType() throws {
        let cases = try Fixtures.cases("schema/fixtures/v0/cases.json")
        let lift = try XCTUnwrap(cases.first { $0.string("name") == "event/session-planned-superset" }?["data"])
        let event = try JSONDecoder().decode(Event.self, from: Fixtures.data(lift))
        XCTAssertEqual(event.type, .sessionPlanned)
        XCTAssertEqual(event.actor.kind, .agent)
        guard case .sessionPlanned(let session) = event.payload else { return XCTFail("wrong payload") }
        XCTAssertEqual(session.blocks.first?.rounds, 4)
        XCTAssertEqual(session.blocks.first?.efforts.first?.metrics?["reps"], Measure(min: 6, max: 8))
        XCTAssertEqual(session.blocks.first?.efforts.first?.metrics?["load"], .exact(10, unit: "kg"))
    }

    func testAnEventOfUnknownTypeDoesNotDecode() throws {
        let cases = try Fixtures.cases("schema/fixtures/v0/cases.json")
        guard case .object(var event)? = cases.first(where: { $0.string("name") == "event/effort-performed-lift" })?["data"] else {
            return XCTFail("fixture missing")
        }
        event["type"] = .string("set.performed")
        XCTAssertThrowsError(try JSONDecoder().decode(Event.self, from: Fixtures.data(.object(event))))
    }

    func testWholeNumbersStayWhole() throws {
        let m: Metrics = ["reps": .exact(5), "load": .exact(102.5, unit: "kg"), "x.grade": .text("V4"),
                          "rir": Measure(min: 1, max: 2)]
        let json = String(decoding: try JSONEncoder().encode(m), as: UTF8.self)
        XCTAssertTrue(json.contains("\"value\":5}") || json.contains("\"value\":5,"), json)
        XCTAssertFalse(json.contains("5.0"), json)
        XCTAssertTrue(json.contains("102.5"), json)
        XCTAssertTrue(json.contains("\"min\":1"), json)
    }

    func testScalarsKeepTheirKind() throws {
        let values = try JSONDecoder().decode([Scalar].self, from: Data("[true, 1, 1.5, \"V4\", false, 0]".utf8))
        XCTAssertEqual(values, [.bool(true), .number(1), .number(1.5), .string("V4"), .bool(false), .number(0)])
    }

    func testMetricRegistryMatchesTheContract() throws {
        guard case .object(let doc) = try Fixtures.json("schema/v0/common.schema.json"),
              case .object(let defs)? = doc["$defs"], case .object(let name)? = defs["metric_name"],
              case .array(let anyOf)? = name["anyOf"], case .object(let first)? = anyOf.first,
              case .array(let names)? = first["enum"] else { return XCTFail("registry not found") }
        XCTAssertEqual(MetricRegistry.all.map { .string($0.name) }, names)
        XCTAssertEqual(MetricRegistry.spec("load")?.units, ["kg", "lb", "%1rm"])
        XCTAssertEqual(MetricRegistry.spec("reps")?.kind, .integer)
        XCTAssertTrue(MetricRegistry.isCustom("x.grade"))
        XCTAssertFalse(MetricRegistry.isCustom("grade"))
    }

    func testMeasureDisplay() {
        XCTAssertEqual(Measure.exact(100, unit: "kg").display, "100 kg")
        XCTAssertEqual(Measure(min: 6, max: 8).display, "6–8")
        XCTAssertEqual(Measure(min: 8).display, "≥ 8")
        XCTAssertEqual(Measure.exact(8.5).display, "8.5")
        XCTAssertEqual(Measure.text("3010").display, "3010")
    }
}
