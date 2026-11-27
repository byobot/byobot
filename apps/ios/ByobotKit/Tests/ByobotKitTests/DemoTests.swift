import XCTest
@testable import ByobotKit

final class DemoTests: XCTestCase {
    func testTheDemoReadsWholeWithASessionTodayAndHistory() {
        let window = Demo.window(today: "2026-11-27")
        XCTAssertTrue(window.unread.isEmpty)
        XCTAssertEqual(window.plannedSessions.map { $0.scheduledOn }, ["2026-11-27", "2026-11-29"])
        let book = makeLogbook(plan: window)
        XCTAssertEqual(book.upNext(today: "2026-11-27")?.id, "demo_lower")
        let last = book.lastTime("barbell_back_squat", at: "example_gym")
        XCTAssertEqual(last.efforts.count, 2)
        XCTAssertEqual(last.locationId, "example_gym")
        XCTAssertTrue(book.pendingEvents.isEmpty)
        let named = window.efforts.map { $0.activityId }
            + window.plannedSessions.flatMap { $0.blocks.flatMap { $0.efforts.map { $0.activityId } } }
        XCTAssertTrue(named.allSatisfy { id in ActivityLibrary.bundled.contains { $0.id == id } }, "\(named)")
    }
}
