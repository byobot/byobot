import XCTest
@testable import ByobotKit

final class DemoTests: XCTestCase {
    func testTheDemoReadsWholeWithASessionTodayAndHistory() {
        let window = Demo.window(today: "2026-11-27")
        XCTAssertTrue(window.unread.isEmpty)
        XCTAssertEqual(window.plannedSessions.map(\.scheduledOn), ["2026-11-27", "2026-11-29"])
        let book = makeLogbook(plan: window)
        XCTAssertEqual(book.upNext(today: "2026-11-27")?.id, "demo_lower")
        let last = book.lastTime("barbell_back_squat", at: "example_gym")
        XCTAssertEqual(last.efforts.count, 2)
        XCTAssertEqual(last.locationId, "example_gym")
        XCTAssertTrue(book.pendingEvents.isEmpty)
    }
}
