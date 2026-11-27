import Foundation
import XCTest
@testable import ByobotKit

/// The generated types against the shared contract fixtures: every event a replica keeps decodes and
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

    func kept() throws -> [(name: String, data: JSONValue)] {
        try Fixtures.cases("schema/fixtures/v1/cases.json").compactMap { item in
            // A later schema_version's events reach only the hub, which relays them as received.
            guard ["accepted", "stored_unprojected"].contains(item.string("outcome")), let data = item["data"],
                  case .object(let event) = data, event["schema_version"] == .number(1) else {
                return nil
            }
            return (item.string("name") ?? "?", data)
        }
    }

    func testEveryKeptEventRoundTrips() throws {
        let cases = try kept()
        for (name, data) in cases { try roundTrip(Event.self, data, name) }
        XCTAssertGreaterThan(cases.count, 25)
    }

    func testEachKindsPayloadRoundTripsAsItsType() throws {
        var checked = Set<String>()
        for (name, data) in try kept() {
            let event = try JSONDecoder().decode(Event.self, from: Fixtures.data(data))
            guard event.op == .put, event.redactedBy == nil, event.kindVersion == KindVersion.all[event.record.kind] else {
                continue
            }
            switch event.record.kind {
            case "activity": try roundTrip(ActivityPayload.self, event.payload, name)
            case "location": try roundTrip(LocationPayload.self, event.payload, name)
            case "setting": try roundTrip(SettingPayload.self, event.payload, name)
            case "plan": try roundTrip(PlanPayload.self, event.payload, name)
            case "planned_session": try roundTrip(PlannedSessionPayload.self, event.payload, name)
            case "session": try roundTrip(SessionPayload.self, event.payload, name)
            case "effort": try roundTrip(EffortPayload.self, event.payload, name)
            case "observation": try roundTrip(ObservationPayload.self, event.payload, name)
            case "overlay": try roundTrip(OverlayPayload.self, event.payload, name)
            default: continue
            }
            checked.insert(event.record.kind)
        }
        XCTAssertEqual(checked.count, 9, "\(checked.sorted())")
    }

    func testAPlannedSupersetReadsAsTyped() throws {
        let cases = try kept()
        let data = try XCTUnwrap(cases.first { $0.name == "planned_session/put" }?.data)
        let event = try JSONDecoder().decode(Event.self, from: Fixtures.data(data))
        let planned = try XCTUnwrap(event.decoded(PlannedSessionPayload.self))
        XCTAssertFalse(planned.blocks.isEmpty)
        XCTAssertNil(Event(id: event.id, schemaVersion: 1, record: event.record, op: .retract, hlc: event.hlc,
                           ts: event.ts, deviceId: event.deviceId, actor: event.actor, prev: [], payload: .object([:]))
            .decoded(PlannedSessionPayload.self), "a retract has no payload to read")
    }

    func testOverlayIdsMatchTheContract() throws {
        guard case .object(let doc) = try Fixtures.json("schema/fixtures/v1/ids.json"),
              case .array(let overlays)? = doc["overlays"] else { return XCTFail("no overlays") }
        XCTAssertFalse(overlays.isEmpty)
        for case .object(let o) in overlays {
            guard case .object(let target)? = o["target"], let effect = o.string("effect").flatMap(OverlayEffect.init),
                  let kind = target.string("kind"), let id = target.string("id") else { return XCTFail("bad fixture") }
            XCTAssertEqual(DerivedId.overlay(effect, kind: kind, id: id), o.string("record_id"), effect.rawValue)
        }
    }

    func testClockReadingsOrderAsText() throws {
        let pattern = try Fixtures.pattern("hlc")
        let at = Date(timeIntervalSince1970: 1_795_000_000)
        let first = HLC.next(after: nil, now: at)
        XCTAssertEqual(first, "1795000000000-000000")
        let second = HLC.next(after: first, now: at.addingTimeInterval(-5))
        XCTAssertEqual(second, "1795000000000-000001", "a clock that went back still moves forward")
        XCTAssertEqual(HLC.next(after: "1795000000000-999999", now: at), "1795000000001-000000")
        XCTAssertEqual(HLC.next(after: second, now: at.addingTimeInterval(1)), "1795000001000-000000")
        for reading in [first, second] { XCTAssertTrue(matches(reading, pattern), reading) }
        XCTAssertLessThan(first, second)
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
        guard case .object(let doc) = try Fixtures.json("schema/v1/registry.json"),
              case .object(let codes)? = doc["codes"] else { return XCTFail("registry not found") }
        XCTAssertEqual(Set(MetricRegistry.all.map(\.name)), Set(codes.keys))
        var healthkit: [String: String] = [:]
        for spec in MetricRegistry.all {
            guard case .object(let code)? = codes[spec.name] else { continue }
            var units: Set<String> = []
            if case .object(let u)? = code["units"] { units = Set(u.keys) }
            XCTAssertEqual(Set(spec.units), units, spec.name)
            XCTAssertEqual(spec.units.first, code.string("unit"), spec.name)
            if case .number(let max)? = code["maximum"] { XCTAssertEqual(spec.maximum, max, spec.name) }
            XCTAssertEqual(spec.exclusiveMinimum, code["exclusive_minimum"] != nil, spec.name)
            if let hk = code.string("healthkit") { healthkit[hk] = spec.name }
        }
        XCTAssertEqual(MetricRegistry.healthkit, healthkit)
        XCTAssertEqual(MetricRegistry.spec("load")?.units, ["kg", "[lb_av]"])
        XCTAssertEqual(MetricRegistry.spec("reps")?.kind, .integer)
        XCTAssertEqual(MetricRegistry.spec("sleep_stage")?.values, HealthMapping.sleepStages.sorted { $0.key < $1.key }.map(\.value))
        XCTAssertTrue(MetricRegistry.isCustom("x.grade"))
        XCTAssertFalse(MetricRegistry.isCustom("grade"))
        XCTAssertFalse(MetricRegistry.isCustom("x.Grade"))
    }

    func testThePlanWindowHasTheContractsFields() throws {
        guard case .object(let doc) = try Fixtures.json("schema/v1/sync.schema.json"),
              case .object(let defs)? = doc["$defs"], case .object(let window)? = defs["plan_window"],
              case .object(let properties)? = window["properties"],
              case .array(let required)? = window["required"] else { return XCTFail("plan_window not found") }
        let full = PlanWindow(from: "2026-11-01", to: "2026-11-30", since: "2026-09-02", storeId: "0193a0b0-0000-7000-8000-00000000c001",
                              hlc: "0000000000001-000000", erased: [], kindVersions: [:], received: [:], rejected: [])
        guard case .object(let written) = try Payloads.encode(full) else { return XCTFail("not an object") }
        XCTAssertEqual(Set(written.keys), Set(properties.keys))
        guard case .object(let bare) = try Payloads.encode(PlanWindow(from: "2026-11-01", to: "2026-11-30")) else {
            return XCTFail("not an object")
        }
        let names = required.compactMap { (item: JSONValue) -> String? in
            if case .string(let s) = item { return s } else { return nil }
        }
        XCTAssertEqual(Set(bare.keys), Set(names))
    }

    func testTheSettingsTheAppWritesAreInTheRegistry() throws {
        guard case .object(let doc) = try Fixtures.json("schema/v1/registry.json"),
              case .object(let settings)? = doc["settings"] else { return XCTFail("registry not found") }
        XCTAssertEqual(Set(SettingId.allCases.map(\.rawValue)), Set(settings.keys))
    }

    func testMeasureDisplay() {
        XCTAssertEqual(Measure.exact(100, unit: "kg").display, "100 kg")
        XCTAssertEqual(Measure.exact(225, unit: "[lb_av]").display, "225 lb")
        XCTAssertEqual(Measure(min: 140, max: 150, unit: "/min").display, "140–150 bpm")
        XCTAssertEqual(Measure(min: 6, max: 8).display, "6–8")
        XCTAssertEqual(Measure(min: 8).display, "≥ 8")
        XCTAssertEqual(Measure.exact(8.5).display, "8.5")
        XCTAssertEqual(Measure.text("3010").display, "3010")
    }
}
