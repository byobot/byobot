import Foundation
import XCTest
@testable import ByobotKit

@MainActor
final class FakeBook: SyncBook {
    var book: Logbook

    init(_ book: Logbook = makeLogbook()) {
        self.book = book
    }

    func change(_ body: (inout Logbook) -> Void) {
        body(&book)
    }
}

/// A laptop that keeps every event it gets, or none when `keeps` is false.
actor FakeHub: SyncTransport {
    var calls: [String] = []
    var keeps = true
    var fails = false
    var received: [String: String] = [:]

    func setKeeps(_ keeps: Bool) { self.keeps = keeps }
    func setFails(_ fails: Bool) { self.fails = fails }

    func push(events: [Event]) async throws -> EventsResponse {
        calls.append("events \(events.count)")
        if fails { throw SyncClientError.unauthorized }
        let results = keeps ? events.indices.map { EventResult(index: $0, status: .accepted) } : []
        for event in events where keeps && event.hlc > (received[event.deviceId] ?? "") {
            received[event.deviceId] = event.hlc
        }
        return EventsResponse(results: results, accepted: results.count, duplicate: 0, storedUnprojected: 0, rejected: 0)
    }

    func plan() async throws -> PlanWindow {
        calls.append("plan")
        return PlanWindow(from: "2026-11-01", to: "2026-12-31", received: received)
    }

    func push(samples: [Sample], deleted: [Deletion]) async throws -> SamplesResponse {
        calls.append("samples \(samples.count) deleted \(deleted.count)")
        if fails { throw SyncClientError.unauthorized }
        return SamplesResponse(written: samples.count, bySource: [:])
    }
}

final class HubSyncTests: XCTestCase {
    static func pending(_ n: Int) -> Logbook {
        var book = makeLogbook()
        let deviceId = book.state.deviceId
        book.state.events = (1...n).map { i -> Event in
            var event = Self.event(i)
            event.deviceId = deviceId
            return event
        }
        book.state.pending = book.state.events.map(\.id)
        return book
    }

    static func event(_ n: Int) -> Event {
        Event(id: "0193a0b0-0000-7000-8000-0000000e\(String(format: "%04d", n))", schemaVersion: 1,
              record: RecordRef(kind: "effort", id: "0193a0b0-0000-7000-8000-0000000f\(String(format: "%04d", n))"),
              op: .put, kindVersion: 1, hlc: String(format: "%016d-0000", n), ts: "2026-11-23T08:00:00-05:00",
              deviceId: "0193a0b0-0000-7000-8000-00000000d002", actor: Actor(kind: .human, id: "me"), prev: [],
              payload: .object([:]))
    }

    static func weighed(_ id: String) -> Sample {
        Sample(source: "apple_health", sourceId: id, type: "HKQuantityTypeIdentifierBodyMass",
               start: "2026-11-23T06:30:00-05:00", value: .number(80), unit: "kg")
    }

    static func health(samples: [String], deleted: [String]) -> SampleBatch {
        SampleBatch(samples: samples.map { Self.weighed($0) },
                    deleted: deleted.map { Deletion(source: "apple_health", sourceId: $0) })
    }

    @MainActor
    func testTheHubPullsThePlanBeforeItPushesAndAgainAfter() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(2))
        try await HubSync(hub).sync(book, health: SampleBatch())
        let calls = await hub.calls
        XCTAssertEqual(calls, ["plan", "events 2", "plan"])
        XCTAssertTrue(book.book.pendingEvents.isEmpty)
        XCTAssertNotNil(book.book.state.plan)
    }

    @MainActor
    func testTheHubPullsOnceWhenNothingWaits() async throws {
        let hub = FakeHub(), book = FakeBook()
        try await HubSync(hub).sync(book, health: SampleBatch())
        let calls = await hub.calls
        XCTAssertEqual(calls, ["plan"])
    }

    @MainActor
    func testTheHubPushesInBatches() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(HubSync.maxBatch + 100))
        try await HubSync(hub).sync(book, health: SampleBatch())
        let calls = await hub.calls
        XCTAssertEqual(calls, ["plan", "events \(HubSync.maxBatch)", "events 100", "plan"])
        XCTAssertTrue(book.book.pendingEvents.isEmpty)
    }

    @MainActor
    func testTheHubStopsPushingWhenNothingIsTaken() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(600))
        await hub.setKeeps(false)
        try await HubSync(hub).sync(book, health: SampleBatch())
        let calls = await hub.calls
        XCTAssertEqual(calls, ["plan", "events 600", "plan"])
        XCTAssertEqual(book.book.pendingEvents.count, 600)
    }

    @MainActor
    func testTheHubSendsAppleHealthInBatches() async throws {
        let hub = FakeHub(), book = FakeBook()
        let ids = (1...HubSync.maxBatch + 500).map { "W\($0)" }
        try await HubSync(hub).sync(book, health: Self.health(samples: ids, deleted: ["D1"]))
        let calls = await hub.calls
        XCTAssertEqual(calls, ["plan", "samples \(HubSync.maxBatch) deleted 0", "samples 500 deleted 0",
                               "samples 0 deleted 1"])
    }

    @MainActor
    func testAHubThatFailsKeepsWhatWaits() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(1))
        await hub.setFails(true)
        do {
            try await HubSync(hub).sync(book, health: Self.health(samples: ["W1"], deleted: []))
            XCTFail("the push should fail")
        } catch {}
        XCTAssertEqual(book.book.pendingEvents.count, 1)
    }
}
