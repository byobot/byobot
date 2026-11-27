import Foundation
import XCTest
@testable import ByobotKit

@MainActor
final class FakeBook: SyncBook {
    var book: Logbook
    var changes = 0

    init(_ book: Logbook = makeLogbook()) {
        self.book = book
    }

    func change(_ body: (inout Logbook) -> Void) {
        body(&book)
        changes += 1
    }
}

/// A laptop that keeps every event it gets, or none when `keeps` is false.
actor FakeHub: SyncTransport {
    var calls: [String] = []
    var keeps = true
    var fails = false

    func setKeeps(_ keeps: Bool) { self.keeps = keeps }
    func setFails(_ fails: Bool) { self.fails = fails }

    func storeId() async throws -> String {
        calls.append("store")
        return "0193a0b0-0000-7000-8000-00000000c001"
    }

    func push(events: [Event]) async throws -> EventsResponse {
        calls.append("events \(events.count)")
        if fails { throw SyncClientError.unauthorized }
        let results = keeps ? events.indices.map { EventResult(index: $0, status: .accepted) } : []
        return EventsResponse(results: results, accepted: results.count, duplicate: 0, storedUnprojected: 0, rejected: 0)
    }

    func plan(from: String?, to: String?) async throws -> PlanWindow {
        calls.append("plan")
        return PlanWindow(from: "2026-11-01", to: "2026-12-31")
    }

    func push(samples: [Sample], deleted: [Deletion]) async throws -> SamplesResponse {
        calls.append("samples \(samples.count) deleted \(deleted.count)")
        if fails { throw SyncClientError.unauthorized }
        return SamplesResponse(written: samples.count, bySource: [:])
    }
}

/// A route that takes Apple Health readings or doesn't, and brings a plan or doesn't.
@MainActor
final class FakeRoute: SyncRoute {
    let held: Set<String>, takes: Bool, plans: Bool
    var got: [SampleBatch] = []
    var takePlan: [Bool] = []

    init(held: Set<String> = [], takes: Bool = true, plans: Bool = true) {
        self.held = held
        self.takes = takes
        self.plans = plans
    }

    func unread() async -> Set<String> { held }

    func sync(_ book: any SyncBook, health: SampleBatch, takePlan: Bool) async -> RouteOutcome {
        got.append(health)
        self.takePlan.append(takePlan)
        var outcome = RouteOutcome()
        outcome.tookHealth = takes
        outcome.planned = plans && takePlan
        return outcome
    }
}

final class RouteTests: XCTestCase {
    static func pending(_ n: Int) -> Logbook {
        var book = makeLogbook()
        book.state.events = (1...n).map { CloudRelayTests.event($0) }
        book.state.pending = book.state.events.map(\.id)
        return book
    }

    static func route(_ k: CloudRelayTests.Keeper, _ folder: MemoryCloudFolder, _ kept: Kept) async throws -> CloudRoute {
        await folder.put(Cloud.epochPath(1), k.epochFile)
        let relay = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
        await folder.put(Cloud.keyPath(1, k.relayID), k.keyFile)
        return CloudRoute(relay: relay, folder: folder, identity: k.relay, save: { await kept.keep($0) })
    }

    static func health(samples: [String], deleted: [String]) -> SampleBatch {
        SampleBatch(samples: samples.map { CloudRelayTests.weighed($0) },
                    deleted: deleted.map { Deletion(source: "apple_health", sourceId: $0) })
    }

    @MainActor
    func testTheHubPullsThePlanBeforeItPushesAndAgainAfter() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(2))
        let outcome = await HubRoute(hub).sync(book, health: SampleBatch(), takePlan: true)
        XCTAssertNil(outcome.error)
        XCTAssertTrue(outcome.planned)
        let calls = await hub.calls
        XCTAssertEqual(calls, ["store", "plan", "events 2", "plan"])
        XCTAssertEqual(book.book.state.storeId, "0193a0b0-0000-7000-8000-00000000c001")
        XCTAssertTrue(book.book.pendingEvents.isEmpty)
        XCTAssertNotNil(book.book.state.plan)
    }

    @MainActor
    func testTheHubPullsOnceWhenNothingWaits() async throws {
        let hub = FakeHub(), book = FakeBook()
        book.book.state.storeId = "0193a0b0-0000-7000-8000-00000000c001"
        _ = await HubRoute(hub).sync(book, health: SampleBatch(), takePlan: true)
        let calls = await hub.calls
        XCTAssertEqual(calls, ["plan"])
    }

    @MainActor
    func testTheHubPushesInBatches() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(CloudRelay.maxBatch + 100))
        _ = await HubRoute(hub).sync(book, health: SampleBatch(), takePlan: true)
        let calls = await hub.calls
        XCTAssertEqual(calls, ["store", "plan", "events \(CloudRelay.maxBatch)", "events 100", "plan"])
        XCTAssertTrue(book.book.pendingEvents.isEmpty)
    }

    @MainActor
    func testTheHubStopsPushingWhenNothingIsTaken() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(600))
        await hub.setKeeps(false)
        let outcome = await HubRoute(hub).sync(book, health: SampleBatch(), takePlan: true)
        XCTAssertNil(outcome.error)
        let calls = await hub.calls
        XCTAssertEqual(calls, ["store", "plan", "events 600", "plan"])
        XCTAssertEqual(book.book.pendingEvents.count, 600)
    }

    @MainActor
    func testTheHubSendsAppleHealthInBatches() async throws {
        let hub = FakeHub(), book = FakeBook()
        let ids = (1...CloudRelay.maxBatch + 500).map { "W\($0)" }
        let outcome = await HubRoute(hub).sync(book, health: Self.health(samples: ids, deleted: ["D1"]), takePlan: true)
        XCTAssertTrue(outcome.tookHealth)
        let calls = await hub.calls
        XCTAssertEqual(calls, ["store", "plan", "samples \(CloudRelay.maxBatch) deleted 0", "samples 500 deleted 0",
                               "samples 0 deleted 1"])
    }

    @MainActor
    func testAHubThatFailsKeepsWhatWaits() async throws {
        let hub = FakeHub(), book = FakeBook(Self.pending(1))
        await hub.setFails(true)
        let outcome = await HubRoute(hub).sync(book, health: Self.health(samples: ["W1"], deleted: []), takePlan: true)
        XCTAssertNotNil(outcome.error)
        XCTAssertFalse(outcome.tookHealth)
        XCTAssertEqual(book.book.pendingEvents.count, 1)
    }

    @MainActor
    func testReadingsGoByTheFirstRouteThatTakesThem() async throws {
        let hub = FakeRoute(), drive = FakeRoute(held: ["D2"])
        let report = await Syncer.run([hub, drive], book: FakeBook(),
                                      health: Self.health(samples: ["W1"], deleted: ["D1", "D2"]))
        XCTAssertTrue(report.healthDone)
        XCTAssertEqual(hub.got, [Self.health(samples: ["W1"], deleted: ["D1"])])
        XCTAssertEqual(drive.got, [Self.health(samples: [], deleted: ["D2"])])
    }

    @MainActor
    func testWhenTheHubFailsDriveTakesEverything() async throws {
        let hub = FakeRoute(takes: false, plans: false), drive = FakeRoute(held: ["D2"])
        let report = await Syncer.run([hub, drive], book: FakeBook(),
                                      health: Self.health(samples: ["W1"], deleted: ["D1", "D2"]))
        XCTAssertTrue(report.healthDone)
        XCTAssertEqual(drive.got, [Self.health(samples: ["W1"], deleted: ["D1", "D2"])])
        XCTAssertEqual(drive.takePlan, [true])
    }

    @MainActor
    func testADeletionDriveHoldsIsUndoneUntilDriveTakesIt() async throws {
        let hub = FakeRoute(), drive = FakeRoute(held: ["D2"], takes: false)
        let report = await Syncer.run([hub, drive], book: FakeBook(),
                                      health: Self.health(samples: ["W1"], deleted: ["D2"]))
        XCTAssertFalse(report.healthDone)
        XCTAssertEqual(hub.got, [Self.health(samples: ["W1"], deleted: [])])
    }

    @MainActor
    func testALaterRouteLeavesAnEarlierPlanAlone() async throws {
        let hub = FakeRoute(), drive = FakeRoute()
        let report = await Syncer.run([hub, drive], book: FakeBook(), health: nil)
        XCTAssertFalse(report.healthDone)
        XCTAssertEqual(hub.takePlan, [true])
        XCTAssertEqual(drive.takePlan, [false])
    }

    @MainActor
    func testDriveDeliversWhatWaitsAndTakesThePlan() async throws {
        var k = try CloudRelayTests.keeper()
        let folder = MemoryCloudFolder(), kept = Kept()
        let route = try await Self.route(k, folder, kept)
        let book = FakeBook(Self.pending(1)), drive = DriveRoute(route)
        _ = await drive.sync(book, health: SampleBatch(), takePlan: true)
        XCTAssertEqual(book.book.pendingEvents.count, 1)
        try await k.publish(folder, read: [1])
        let outcome = await drive.sync(book, health: SampleBatch(), takePlan: false)
        XCTAssertNil(outcome.error)
        XCTAssertFalse(outcome.planned)
        XCTAssertNil(book.book.state.plan)
        XCTAssertTrue(book.book.pendingEvents.isEmpty)
        let again = await drive.sync(book, health: SampleBatch(), takePlan: true)
        XCTAssertTrue(again.planned)
        XCTAssertNotNil(book.book.state.plan)
    }

    @MainActor
    func testDriveLeftMidSyncDoesNothing() async throws {
        let k = try CloudRelayTests.keeper()
        let folder = MemoryCloudFolder(), kept = Kept()
        let route = try await Self.route(k, folder, kept)
        let book = FakeBook(Self.pending(1)), drive = DriveRoute(route, isCurrent: { false })
        let outcome = await drive.sync(book, health: Self.health(samples: ["W1"], deleted: []), takePlan: true)
        XCTAssertFalse(outcome.tookHealth)
        let unread = await route.unread
        XCTAssertEqual(unread, [])
        XCTAssertEqual(book.changes, 0)
    }
}
