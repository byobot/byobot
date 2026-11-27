import Foundation
import XCTest
@testable import ByobotKit

final class StateFileTests: XCTestCase {
    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func testSavedStateComesBack() throws {
        var file = StateFile(url: dir.appendingPathComponent("state.json"))
        let first = file.load()
        XCTAssertEqual(first.outcome, .new)
        var book = makeLogbook()
        book.logEffort(activityId: "run", metrics: [:])
        try file.save(book.state)
        var again = StateFile(url: file.url)
        let loaded = again.load()
        XCTAssertEqual(loaded.outcome, .loaded)
        XCTAssertEqual(loaded.state, book.state)
    }

    func testAnUnreadableFileIsMovedAsideNeverOverwritten() throws {
        let url = dir.appendingPathComponent("state.json")
        let old = Data(#"{"deviceId": 7, "events": "from a newer build"}"#.utf8)
        try old.write(to: url)
        var file = StateFile(url: url)
        let loaded = file.load()
        guard case .setAside(let aside) = loaded.outcome else { return XCTFail("\(loaded.outcome)") }
        XCTAssertEqual(aside.lastPathComponent, "state.json.unreadable")
        XCTAssertEqual(try Data(contentsOf: aside), old)
        XCTAssertTrue(loaded.state.events.isEmpty)
        try file.save(loaded.state)
        XCTAssertEqual(try Data(contentsOf: aside), old, "saving the fresh state leaves the old file alone")

        let second = Data("not json".utf8)
        try second.write(to: url)
        var later = StateFile(url: url)
        guard case .setAside(let next) = later.load().outcome else { return XCTFail("not set aside") }
        XCTAssertEqual(next.lastPathComponent, "state.json.unreadable-2")
        XCTAssertEqual(try Data(contentsOf: aside), old, "an earlier one set aside stays as it was")
        XCTAssertEqual(try Data(contentsOf: next), second)
    }

    func testAFileThatCantBeReadIsLeftAndNotSavedOver() throws {
        let url = dir.appendingPathComponent("state.json")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)  // there, not readable
        var file = StateFile(url: url)
        XCTAssertEqual(file.load().outcome, .cantRead)
        XCTAssertThrowsError(try file.save(AppState())) { XCTAssertTrue($0 is StateFile.Locked) }
        var isDir: ObjCBool = false
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue)
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appendingPathComponent("state.json.unreadable").path))
    }
}
