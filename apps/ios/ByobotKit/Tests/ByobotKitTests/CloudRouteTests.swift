#if canImport(CryptoKit) && canImport(Compression)
import Foundation
import XCTest
@testable import ByobotKit

actor Kept {
    var relay: CloudRelay?
    var saves = 0
    func keep(_ r: CloudRelay) { relay = r; saves += 1 }
}

final class CloudRouteTests: XCTestCase {
    func makeRoute(_ k: CloudRelayTests.Keeper, _ folder: MemoryCloudFolder, _ kept: Kept) async throws -> CloudRoute {
        let relay = try await CloudRelay.join(folder, link: k.link, deviceID: k.relayID, identity: k.relay)
        await folder.put(Cloud.epochPath(1), k.epochFile)
        await folder.put(Cloud.keyPath(1, k.relayID), k.keyFile)
        return CloudRoute(relay: relay, folder: folder, identity: k.relay, save: { await kept.keep($0) })
    }

    func testEventsAreDoneOnceTheMacReadsThem() async throws {
        var k = try CloudRelayTests.keeper()
        let folder = MemoryCloudFolder(), kept = Kept()
        let route = try await makeRoute(k, folder, kept)
        let event = CloudRelayTests.event(1)
        var result = try await route.sync(events: [event])
        XCTAssertEqual(result.delivered, [])
        try await k.publish(folder, read: [1])
        result = try await route.sync(events: [])
        XCTAssertEqual(result.delivered, [event.id])
        XCTAssertNotNil(result.plan)
        let saved = await kept.relay, relay = await route.relay
        XCTAssertEqual(saved, relay)
    }

    func testASampleTheMacHasntReadIsUnreadUntilItIs() async throws {
        var k = try CloudRelayTests.keeper()
        let folder = MemoryCloudFolder(), kept = Kept()
        let route = try await makeRoute(k, folder, kept)
        _ = try await route.sync(events: [], samples: [CloudRelayTests.weighed("W1")])
        var unread = await route.unread
        XCTAssertEqual(unread, ["W1"])
        try await k.publish(folder, read: [1])
        _ = try await route.sync(events: [])
        unread = await route.unread
        XCTAssertEqual(unread, [])
    }

    func testTheLogbookDropsWhatTheMacRead() {
        var book = Logbook(state: AppState(), origin: Origin(app: "byobot_ios", version: nil, device: "phone"))
        _ = book.startSession(planned: nil, name: "Test")
        let ids = Set(book.pendingEvents.map(\.id))
        XCTAssertFalse(ids.isEmpty)
        book.delivered(ids)
        XCTAssertTrue(book.pendingEvents.isEmpty)
    }
}
#endif
