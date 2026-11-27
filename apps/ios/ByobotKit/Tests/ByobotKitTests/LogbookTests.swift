import Foundation
import XCTest
@testable import ByobotKit

/// A clock that moves one second per reading, so every event has its own time.
final class TickingClock: @unchecked Sendable {
    var now = Date(timeIntervalSince1970: 1_795_000_000)
    func read() -> Date {
        now += 1
        return now
    }
}

func makeLogbook(plan: PlanWindow? = nil) -> Logbook {
    let clock = TickingClock()
    var book = Logbook(state: AppState(deviceId: "0193a0b0-0000-7000-8000-00000000ab01"),
                       origin: Origin(app: "byobot_ios", version: "0.1.0", device: "Example iPhone"),
                       clock: { clock.read() })
    if let plan { book.applyPlan(plan) }
    return book
}

final class LogbookTests: XCTestCase {
    func plannedSession() throws -> PlannedSession {
        try XCTUnwrap(try Fixtures.window().plannedSessions.first { $0.id == "sess-upper" })
    }

    func window(efforts: [PerformedEffort]) throws -> PlanWindow {
        var window = try Fixtures.window()
        window.efforts = efforts
        return window
    }

    func remote(_ part: String, metrics: Metrics) -> PerformedEffort {
        PerformedEffort(id: "remote-1", activityId: "pull_up", performedAt: "2026-11-26T07:00:00Z",
                        session: "done-elsewhere", planned: "sess-upper", part: part, round: 1, metrics: metrics)
    }

    func testASessionWritesContractShapedEvents() throws {
        var book = makeLogbook(plan: try Fixtures.window())
        let planned = try plannedSession()
        let session = book.startSession(planned: planned)
        let pullUp = planned.blocks[0].efforts[0]
        let set = book.logEffort(activityId: pullUp.activityId, metrics: ["reps": .exact(7), "load": .exact(10, unit: "kg")],
                                 plannedEffortId: pullUp.id, round: 1)
        let finished = try XCTUnwrap(book.finishSession())

        XCTAssertEqual(finished.id, session.id)
        XCTAssertNotNil(finished.endedAt)
        XCTAssertEqual(finished.status, .completed)
        XCTAssertEqual(finished.plannedSessionId, "sess-upper")
        XCTAssertNil(finished.planId, "this planned session is in no plan")
        XCTAssertNil(book.activeSession)
        let events = book.state.events
        XCTAssertEqual(events.map(\.record), [RecordRef(kind: "session", id: session.id), RecordRef(kind: "effort", id: set.id),
                                              RecordRef(kind: "session", id: session.id)])
        XCTAssertEqual(events.map(\.op), [.put, .put, .put])
        XCTAssertEqual(events.map(\.prev), [[], [], [events[0].id]], "finishing names the start it replaces")
        XCTAssertEqual(book.pendingEvents.map(\.id), events.map(\.id))
        XCTAssertEqual(events.map(\.hlc), events.map(\.hlc).sorted())
        XCTAssertEqual(Set(events.map(\.hlc)).count, 3)

        // Every id, clock and time is in the contract's format, and the JSON uses the contract's names.
        let uuid = try Fixtures.pattern("uuid"), ts = try Fixtures.pattern("timestamp"), id = try Fixtures.pattern("id")
        let hlc = try Fixtures.pattern("hlc"), zone = try Fixtures.pattern("zone")
        for event in events {
            XCTAssertTrue(matches(event.id, uuid), event.id)
            XCTAssertTrue(matches(event.record.id, uuid), event.record.id)
            XCTAssertTrue(matches(event.ts, ts), event.ts)
            XCTAssertTrue(matches(event.hlc, hlc), event.hlc)
            XCTAssertTrue(matches(event.deviceId, id))
            XCTAssertEqual(event.kindVersion, 1)
        }
        let request = try JSONValue.parse(JSONEncoder().encode(EventsRequest(events: book.pendingEvents)))
        guard case .object(let body) = request, case .array(let sent)? = body["events"],
              case .object(let effortEvent) = sent[1], case .object(let payload)? = effortEvent["payload"],
              case .object(let time)? = payload["time"] else {
            return XCTFail("unexpected shape")
        }
        XCTAssertEqual(effortEvent["schema_version"], .number(1))
        XCTAssertEqual(effortEvent["op"], .string("put"))
        XCTAssertEqual(effortEvent["record"], .object(["kind": .string("effort"), "id": .string(set.id)]))
        XCTAssertEqual(effortEvent["actor"], .object(["kind": .string("human"), "id": .string("me")]))
        XCTAssertNil(effortEvent["reverts"])
        XCTAssertEqual(payload["links"], .object([
            "part_of": .object(["kind": .string("session"), "id": .string(session.id)]),
            "answers": .object(["kind": .string("planned_session"), "id": .string("sess-upper"), "part": .string(pullUp.id)]),
        ]))
        XCTAssertEqual(payload["round"], .number(1))
        XCTAssertEqual(payload["modality"], .string("self_reported"))
        XCTAssertEqual(payload["status"], .string("completed"))
        XCTAssertEqual(payload["origin"], .object(["app": .string("byobot_ios"), "version": .string("0.1.0"),
                                                   "device": .string("Example iPhone")]))
        XCTAssertTrue(matches(time.string("start") ?? "", ts))
        if let z = time.string("zone") { XCTAssertTrue(matches(z, zone), z) }
        XCTAssertEqual(payload["metrics"], .object(["reps": .object(["value": .number(7)]),
                                                    "load": .object(["value": .number(10), "unit": .string("kg")])]))
        guard case .object(let start) = sent[0], case .object(let started)? = start["payload"] else { return XCTFail() }
        XCTAssertEqual(started["status"], .string("in_progress"))
        XCTAssertEqual(started["links"], .object(["answers": .object(["kind": .string("planned_session"),
                                                                       "id": .string("sess-upper")])]))
    }

    func testLoggedRoundsMergeThisPhoneAndTheLaptop() throws {
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0], dip = planned.blocks[0].efforts[1]
        // The laptop already has round 1 of the pull-ups, logged elsewhere.
        var book = makeLogbook(plan: try window(efforts: [remote(pullUp.id, metrics: ["reps": .exact(8)])]))
        book.startSession(planned: planned)
        book.logEffort(activityId: "pull_up", metrics: ["reps": .exact(6)], plannedEffortId: pullUp.id, round: 2)
        let dip1 = book.logEffort(activityId: "dip", metrics: ["reps": .exact(10)], plannedEffortId: dip.id, round: 1)

        var logged = book.logged(for: planned)
        XCTAssertEqual(logged[pullUp.id]?[1]?.id, "remote-1")
        XCTAssertEqual(logged[pullUp.id]?[2]?.metrics?["reps"], .exact(6))
        XCTAssertEqual(logged[dip.id]?[1]?.id, dip1.id)
        XCTAssertEqual(planned.plannedCount, 8)
        let other = try XCTUnwrap(try Fixtures.window().plannedSessions.first { $0.id == "sess-lower-a" })
        XCTAssertEqual(book.logged(for: other), [:], "sets answer the planned session they were logged against")

        // Undo is an event too: a retract naming the version it takes back.
        let undo = book.undoEffort(id: dip1.id)
        XCTAssertEqual(undo.map(\.op), [.retract])
        XCTAssertEqual(undo.first?.reverts, dip1.prev.first)
        XCTAssertEqual(undo.first?.prev, dip1.prev)
        XCTAssertEqual(undo.first?.payload, .object([:]))
        XCTAssertNil(undo.first?.kindVersion)
        logged = book.logged(for: planned)
        XCTAssertNil(logged[dip.id]?[1])
        XCTAssertEqual(book.undoEffort(id: dip1.id), [], "already undone")
        XCTAssertEqual(book.efforts(inSession: book.state.activeSessionId!).count, 1)
    }

    func testPushResultsClearTheOutbox() throws {
        var book = makeLogbook()
        book.startSession(name: "Easy run")
        let a = book.logEffort(activityId: "run", metrics: ["distance": .exact(5, unit: "km")])
        book.logEffort(activityId: "run", metrics: ["distance": .exact(1, unit: "km")])
        book.logEffort(activityId: "run", metrics: ["distance": .exact(2, unit: "km")])
        let sent = book.pendingEvents
        XCTAssertEqual(sent.count, 4)
        let response = EventsResponse(results: [
            EventResult(index: 0, id: sent[0].id, status: .accepted),
            EventResult(index: 1, id: sent[1].id, status: .duplicate),
            EventResult(index: 2, id: sent[2].id, status: .storedUnprojected),
            EventResult(index: 3, id: sent[3].id, status: .rejected, reason: "nope"),
        ], accepted: 1, duplicate: 1, storedUnprojected: 1, rejected: 1)
        // Written while the push was in flight: stays pending.
        book.logEffort(activityId: "run", metrics: [:])
        book.applyPush(response, sent: sent)
        XCTAssertEqual(book.pendingEvents.count, 1)
        XCTAssertEqual(book.state.rejected.map(\.eventId), [sent[3].id])
        XCTAssertEqual(book.state.rejected.map(\.reason), ["nope"])
        XCTAssertEqual(book.localEfforts.first?.id, a.id)
        XCTAssertNil(book.localEfforts.last?.metrics, "no metrics means none, not an empty map")
    }

    func testWritesComeAfterTheLaptopsClock() throws {
        var book = makeLogbook()
        var window = try Fixtures.window()
        window.hlc = "1900000000000-000004"
        book.applyPlan(window)
        XCTAssertEqual(book.state.hlc, "1900000000000-000004")
        book.logEffort(activityId: "run", metrics: [:])
        XCTAssertGreaterThan(try XCTUnwrap(book.state.events.last).hlc, "1900000000000-000004")
        window.hlc = "7258118400000-000000"
        book.applyPlan(window)
        XCTAssertLessThan(book.state.hlc ?? "", HLC.ceiling, "a reading past 2200 is ignored")
    }

    func testWhatWasErasedLeavesThePhone() throws {
        var book = makeLogbook()
        let session = book.startSession(name: "Easy run")
        let kept = book.logEffort(activityId: "run", metrics: [:])
        let erased = book.logEffort(activityId: "run", metrics: ["distance": .exact(5, unit: "km")])
        var window = try Fixtures.window()
        window.erased = [ErasedRecord(kind: "effort", id: erased.id), ErasedRecord(kind: "session", id: session.id)]
        book.applyPlan(window)
        XCTAssertEqual(book.localEfforts.map(\.id), [kept.id])
        XCTAssertFalse(book.pendingEvents.contains { $0.record.id == erased.id || $0.record.id == session.id })
        XCTAssertNil(book.state.heads?["effort/\(erased.id)"])
        XCTAssertNil(book.activeSession)
        XCTAssertFalse(book.canUndo(erased.id))
    }

    func testALongErasedListLeavesInOnePass() throws {
        var book = makeLogbook()
        book.logEffort(activityId: "run", metrics: [:])
        let template = try XCTUnwrap(book.state.events.last)
        book.state.events = (0..<20_000).map { i in
            var event = template
            event.id = "event-\(i)"
            event.record = RecordRef(kind: "effort", id: "set-\(i)")
            return event
        }
        book.state.pending = book.state.events.map(\.id)
        var window = try Fixtures.window()
        window.erased = (0..<1_000).map { ErasedRecord(kind: "effort", id: "set-\($0 * 2)") }
            + (0..<1_000).map { ErasedRecord(kind: "session", id: "not-here-\($0)") }
        let start = Date()
        book.applyPlan(window)
        XCTAssertLessThan(Date().timeIntervalSince(start), 5)
        XCTAssertEqual(book.state.events.count, 19_000)
        XCTAssertEqual(Array(book.state.pending.prefix(3)), ["event-1", "event-3", "event-5"])
    }

    func testRefusalsWhoseAnswerWasLostAreSetAside() throws {
        var book = makeLogbook()
        book.logEffort(activityId: "run", metrics: [:])
        let sent = try XCTUnwrap(book.pendingEvents.first)
        var window = try Fixtures.window()
        window.rejected = [Rejection(id: sent.id, reason: "nope"), Rejection(id: "not-from-here", reason: "nope")]
        book.applyPlan(window)
        book.applyPlan(window)
        XCTAssertEqual(book.pendingEvents, [])
        XCTAssertEqual(book.state.rejected.map(\.eventId), [sent.id])
        XCTAssertEqual(book.state.rejected.map(\.reason), ["nope"])
    }

    func ack(_ book: inout Logbook) {
        let sent = book.pendingEvents
        let results = sent.enumerated().map { EventResult(index: $0.offset, id: $0.element.id, status: .accepted) }
        book.applyPush(EventsResponse(results: results, accepted: sent.count, duplicate: 0, storedUnprojected: 0,
                                      rejected: 0), sent: sent)
    }

    func testALaptopRestoredFromAnOlderBackupGetsWhatItLost() throws {
        var book = makeLogbook()
        book.paired(storeId: "store-a")
        let first = book.logEffort(activityId: "run", metrics: [:])
        book.logEffort(activityId: "run", metrics: [:])
        let last = book.logEffort(activityId: "run", metrics: [:])
        ack(&book)
        let events = book.state.events
        var window = try Fixtures.window()
        XCTAssertEqual(book.applyPlan(window), 0, "an older laptop says nothing about what it holds")
        window.received = [book.state.deviceId: events[0].hlc]
        XCTAssertEqual(book.applyPlan(window), 2)
        XCTAssertEqual(book.pendingEvents.map(\.id), Array(events[1...].map(\.id)))
        XCTAssertEqual(book.prev("effort", last.id), [events[2].id], "a change builds on what's being resent")
        XCTAssertEqual(book.applyPlan(window), 0, "queued once")
        ack(&book)
        window.received = [book.state.deviceId: events[2].hlc]
        XCTAssertEqual(book.applyPlan(window), 0)
        book.paired(storeId: "store-a")
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 3, "re-pairing after a restore keeps the same store")
        XCTAssertEqual(book.pendingEvents.first?.record.id, first.id)
    }

    func testAnotherStoreGetsOnlyWhatIsWaitingAndWhatComesNext() throws {
        var book = makeLogbook()
        book.paired(storeId: "store-a")
        book.logEffort(activityId: "run", metrics: [:])
        ack(&book)
        let waiting = book.logEffort(activityId: "run", metrics: [:])
        book.paired(storeId: "store-b")
        let next = book.logEffort(activityId: "run", metrics: [:])
        var window = try Fixtures.window()
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 0)
        XCTAssertEqual(book.pendingEvents.map(\.record.id), [waiting.id, next.id])
    }

    func testWhatALaptopLostIsFoundBeforeTheNextPush() throws {
        var book = makeLogbook()
        book.paired(storeId: "store-a")
        book.logEffort(activityId: "run", metrics: [:])
        ack(&book)
        let lost = try XCTUnwrap(book.state.events.last)
        book.logEffort(activityId: "run", metrics: [:])  // waiting, so the laptop hasn't seen it
        var window = try Fixtures.window()
        window.received = [:]  // restored from before the first set
        XCTAssertEqual(book.applyPlan(window), 1)
        XCTAssertEqual(book.pendingEvents.first?.id, lost.id)
    }

    func testWhatALaptopTookBeforeItsAnswerWasLostStaysThere() throws {
        var book = makeLogbook()
        book.paired(storeId: "store-a")
        book.logEffort(activityId: "run", metrics: [:])
        let taken = book.logEffort(activityId: "run", metrics: [:])  // pushed; the answer never came
        let waiting = book.logEffort(activityId: "run", metrics: [:])
        var window = try Fixtures.window()
        window.received = [book.state.deviceId: try XCTUnwrap(book.lastEvent("effort", taken.id)).hlc]
        book.settle(with: window)
        XCTAssertEqual(book.pendingEvents.map(\.record.id), [waiting.id])
        book.paired(storeId: "store-b")
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 0)
        XCTAssertEqual(book.pendingEvents.map(\.record.id), [waiting.id], "the new store gets only what's waiting")
    }

    func testAnInstallPairedBeforeTheStoreIdWasKeptSendsNothingOld() throws {
        var book = makeLogbook()
        book.logEffort(activityId: "run", metrics: [:])
        ack(&book)
        book.paired(storeId: "store-b")  // learned from the laptop it's paired with now
        var window = try Fixtures.window()
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 0, "it may have sent those to another store")
    }

    func testAnOlderInstallBackOnItsRestoredLaptopGetsWhatItLost() throws {
        var book = makeLogbook()
        for _ in 0..<3 { book.logEffort(activityId: "run", metrics: [:]) }
        ack(&book)
        let events = book.state.events
        book.paired(storeId: "store-a")  // its own laptop is gone; this one was restored from a backup
        var window = try Fixtures.window()
        window.received = [book.state.deviceId: events[0].hlc]
        XCTAssertEqual(book.applyPlan(window), 2)
        XCTAssertEqual(book.pendingEvents.map(\.id), Array(events[1...].map(\.id)))
        ack(&book)
        book.paired(storeId: "store-b")
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 0, "once it knows its store, another one gets nothing old")
    }

    func testAnOlderInstallMovingOnBeforeItsFirstPlanSendsNothingOld() throws {
        var book = makeLogbook()
        book.logEffort(activityId: "run", metrics: [:])
        ack(&book)
        book.paired(storeId: "store-a")
        book.paired(storeId: "store-b")
        var window = try Fixtures.window()
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 0)
    }

    /// A new laptop set up as start `serve`, pair, then restore the backup: pairing again sends what the old
    /// laptop took after the backup, and nothing that went only to the empty store.
    func testPairingAgainAfterARestoreSendsWhatTheBackupMissed() throws {
        var book = makeLogbook()
        book.paired(storeId: "store-a")
        for _ in 0..<10 { book.logEffort(activityId: "run", metrics: [:]) }
        ack(&book)
        let old = book.state.events
        book.paired(storeId: "store-new")  // the new laptop's empty store
        var window = try Fixtures.window()
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 0, "the empty store gets nothing old")
        let onlyThere = book.logEffort(activityId: "run", metrics: [:])
        ack(&book)

        book.paired(storeId: "store-a")  // restored from a backup holding the first six
        window.received = [book.state.deviceId: old[5].hlc]
        XCTAssertEqual(book.applyPlan(window), 4)
        XCTAssertEqual(book.pendingEvents.map(\.id), Array(old[6...].map(\.id)))
        XCTAssertFalse(book.pendingEvents.contains { $0.record.id == onlyThere.id })
        XCTAssertEqual(book.applyPlan(window), 0, "queued once")
        ack(&book)

        book.paired(storeId: "store-new")
        window.received = [:]
        XCTAssertEqual(book.applyPlan(window), 1, "back on the other store, only what it took")
        XCTAssertEqual(book.pendingEvents.map(\.record.id), [onlyThere.id])
    }

    func testCorrectingASetKeepsItsTimeAndRound() throws {
        var book = makeLogbook(plan: try Fixtures.window())
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0]
        book.startSession(planned: planned)
        let first = book.logEffort(activityId: "pull_up", metrics: ["reps": .exact(8)], plannedEffortId: pullUp.id, round: 2)
        let fixed = book.editEffort(first, metrics: ["reps": .exact(6)], side: nil, notes: "miscounted")

        XCTAssertEqual(fixed.id, first.id)
        XCTAssertEqual(fixed.performedAt, first.performedAt)
        XCTAssertEqual(fixed.round, 2)
        XCTAssertEqual(fixed.sessionId, first.sessionId)
        XCTAssertEqual(fixed.plannedEffortId, pullUp.id)
        XCTAssertEqual(book.localEfforts, [fixed])
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[2], fixed)
        let edits = book.pendingEvents.filter { $0.record.kind == "effort" }
        XCTAssertEqual(edits.count, 2, "the correction is a new event")
        XCTAssertEqual(edits[1].prev, first.prev, "naming the version it corrects")
        XCTAssertGreaterThan(edits[1].hlc, edits[0].hlc)
        XCTAssertFalse(book.loggedElsewhere(first.id))

        let moved = book.editEffort(fixed, metrics: ["reps": .exact(6)], side: nil, notes: nil,
                                    performedAt: "2026-11-26T07:05:00-05:00")
        XCTAssertEqual(moved.performedAt, "2026-11-26T07:05:00-05:00")
        XCTAssertNil(moved.notes)
    }

    func testCorrectingASetFromAnotherDeviceAndUndoingIt() throws {
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0]
        let remote = remote(pullUp.id, metrics: ["reps": .exact(8)])
        var book = makeLogbook(plan: try window(efforts: [remote]))
        let fixed = book.editEffort(remote, metrics: ["reps": .exact(7)], side: .both, notes: nil)
        XCTAssertEqual(fixed.performedAt, remote.performedAt)
        XCTAssertEqual(book.pendingEvents.last?.prev, remote.prev, "a change made elsewhere meanwhile shows as a conflict")
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1], fixed)
        XCTAssertTrue(book.loggedElsewhere(remote.id))
        XCTAssertTrue(book.canUndo(remote.id))

        let undo = book.undoEffort(id: remote.id)
        XCTAssertEqual(undo.map(\.op), [.put])
        XCTAssertEqual(undo.first?.reverts, fixed.prev.first)
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1]?.payload, remote.payload, "back to the other device's version")
        XCTAssertFalse(book.canUndo(remote.id))
    }

    func testASetANewerAppWroteIsLeftAsItIs() throws {
        let planned = try plannedSession()
        var remote = remote(planned.blocks[0].efforts[0].id, metrics: ["reps": .exact(8)])
        remote.kindVersion = (KindVersion.all["effort"] ?? 1) + 1
        var book = makeLogbook(plan: try window(efforts: [remote]))
        XCTAssertTrue(book.isNewer("effort", remote.id))
        XCTAssertEqual(book.editEffort(remote, metrics: ["reps": .exact(7)], side: nil, notes: nil), remote)
        XCTAssertTrue(book.pendingEvents.isEmpty, "changing it here would drop what this app can't read")
    }

    func testValuesFromANewerByobotStillRead() throws {
        XCTAssertEqual(try JSONDecoder().decode([Side].self, from: Data(#"["left", "diagonal"]"#.utf8)), [.left, .unknown])
        XCTAssertFalse(Side.allCases.contains(.unknown))
        XCTAssertEqual(KindVersion.lowest("effort", .object(["round": .number(1)])), 1)
    }

    func testThePulledVersionWinsOnceThePhoneHasSynced() throws {
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0]
        let remote = remote(pullUp.id, metrics: ["reps": .exact(8)])
        var book = makeLogbook(plan: try window(efforts: [remote]))
        let mine = book.editEffort(remote, metrics: ["reps": .exact(7)], side: nil, notes: nil)
        book.applyPlan(try window(efforts: [remote]))
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1], mine, "not pushed yet, so the phone's is newer")
        XCTAssertEqual(book.prev("effort", remote.id), mine.prev)

        let sent = book.pendingEvents
        book.applyPush(EventsResponse(results: sent.enumerated().map { EventResult(index: $0.offset, id: $0.element.id, status: .accepted) },
                                      accepted: sent.count, duplicate: 0, storedUnprojected: 0, rejected: 0), sent: sent)
        var laterElsewhere = remote
        laterElsewhere.metrics = ["reps": .exact(9)]
        laterElsewhere.prev = ["pulled-later"]
        book.applyPlan(try window(efforts: [laterElsewhere]))
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1], laterElsewhere)
        XCTAssertEqual(book.prev("effort", remote.id), ["pulled-later"], "the next change builds on the laptop's version")
    }

    func testUndoRemovesASetLoggedHereWithItsCorrections() throws {
        var book = makeLogbook(plan: try Fixtures.window())
        let planned = try plannedSession()
        let dip = planned.blocks[0].efforts[1]
        book.startSession(planned: planned)
        let first = book.logEffort(activityId: "dip", metrics: ["reps": .exact(10)], plannedEffortId: dip.id, round: 1)
        book.editEffort(first, metrics: ["reps": .exact(9)], side: nil, notes: nil)
        XCTAssertEqual(book.undoEffort(id: first.id).map(\.op), [.retract])
        XCTAssertNil(book.logged(for: planned)[dip.id]?[1])
        XCTAssertEqual(book.localEfforts, [])
        XCTAssertNil(book.state.remoteOriginals)
    }

    func testSettingsAreRecordsInTheLog() throws {
        var book = makeLogbook(plan: try Fixtures.window())
        book.setEquipment(["rack", "barbell"])
        book.setLocation("home")
        let settings = book.state.events.filter { $0.record.kind == "setting" }
        XCTAssertEqual(settings.map(\.record.id), ["equipment", "current_location"])
        XCTAssertEqual(settings.map(\.payload), [
            .object(["value": .array([.string("barbell"), .string("rack")])]),
            .object(["value": .string("home")]),
        ])
        book.setLocation("home")
        XCTAssertEqual(book.state.events.count, 2, "no change, no event")
        book.setEquipment(nil)
        XCTAssertEqual(book.state.events.last?.payload, .object(["value": .null]))
        XCTAssertEqual(book.state.events.last?.prev, [settings[0].id])

        // Once sent, what the laptop says wins.
        let sent = book.pendingEvents
        book.applyPush(EventsResponse(results: sent.enumerated().map { EventResult(index: $0.offset, id: $0.element.id, status: .accepted) },
                                      accepted: sent.count, duplicate: 0, storedUnprojected: 0, rejected: 0), sent: sent)
        var window = try Fixtures.window()
        window.settings = [Setting(id: "equipment", payload: SettingPayload(value: .array([.string("cable")])), prev: ["e9"]),
                           Setting(id: "current_location", payload: SettingPayload(value: .string("hotel")), prev: ["e10"])]
        book.applyPlan(window)
        XCTAssertEqual(book.equipment, ["cable"])
        XCTAssertEqual(book.currentLocationId, "hotel")
        XCTAssertEqual(book.prev("setting", "equipment"), ["e9"])
    }

    func testStateSurvivesSaving() throws {
        var book = makeLogbook(plan: try Fixtures.window())
        book.startSession(name: "Mobility")
        book.logEffort(activityId: "couch_stretch", metrics: ["duration": .exact(90, unit: "s")], side: .left)
        book.setOrder(["e-dip", "e-pullup"], for: "sess-upper")
        book.setEquipment(["barbell"])
        let data = try JSONEncoder().encode(book.state)
        XCTAssertEqual(try JSONDecoder().decode(AppState.self, from: data), book.state)
    }
}

final class EffortFormTests: XCTestCase {
    func testFieldsStartFromThePlan() {
        let planned = PlannedEffort(id: "e", activityId: "barbell_back_squat",
                                    metrics: ["load": .exact(100, unit: "kg"), "reps": Measure(min: 3, max: 5)])
        let activity = Activity(id: "barbell_back_squat", name: "Squat", defaultMetrics: ["reps", "load", "rpe"])
        let fields = EffortForm.fields(planned: planned, activity: activity)
        XCTAssertEqual(fields.map(\.name), ["reps", "load", "rpe"])
        XCTAssertEqual(fields[0].text, "", "a range is a hint, not a value")
        XCTAssertEqual(fields[0].target?.display, "3–5")
        XCTAssertEqual(fields[1].text, "100")
        XCTAssertEqual(fields[1].unit, "kg")
        XCTAssertEqual(fields[1].units, ["kg", "[lb_av]"])
        XCTAssertNil(fields[2].target)
    }

    func testCorrectingStartsFromWhatWasLogged() {
        let planned = PlannedEffort(id: "e", activityId: "barbell_back_squat", metrics: ["load": .exact(100, unit: "kg")])
        let activity = Activity(id: "barbell_back_squat", name: "Squat", defaultMetrics: ["reps", "load", "rpe"])
        let fields = EffortForm.fields(planned: planned, activity: activity,
                                       filledFrom: ["reps": .exact(5), "load": .exact(225, unit: "[lb_av]"), "x.grade": .text("V4")])
        XCTAssertEqual(fields.map(\.name), ["load", "reps", "rpe", "x.grade"])
        XCTAssertEqual(fields.map(\.text), ["225", "5", "", "V4"])
        XCTAssertEqual(fields[0].unit, "[lb_av]")
        XCTAssertEqual(fields[0].target?.display, "100 kg", "the plan stays visible")
    }

    func testFilledFieldsBecomeMetrics() throws {
        var fields = [
            EffortForm.field(name: "reps", target: nil),
            EffortForm.field(name: "load", target: nil),
            EffortForm.field(name: "rpe", target: nil),
            EffortForm.field(name: "tempo", target: nil),
            EffortForm.field(name: "x.grade", target: nil),
            EffortForm.field(name: "x.band", target: nil),
        ]
        fields[0].text = "5"
        fields[1].text = "225"
        fields[1].unit = "[lb_av]"
        fields[3].text = "3010"
        fields[4].text = "V4"
        fields[5].text = "12,5"
        let metrics = try EffortForm.metrics(from: fields)
        XCTAssertEqual(metrics, ["reps": .exact(5), "load": .exact(225, unit: "[lb_av]"), "tempo": .text("3010"),
                                 "x.grade": .text("V4"), "x.band": .exact(12.5)])
    }

    func testBadInputIsRefusedBeforeItReachesTheLaptop() {
        var reps = EffortForm.field(name: "reps", target: nil)
        reps.text = "5.5"
        XCTAssertThrowsError(try EffortForm.metrics(from: [reps])) { XCTAssertEqual($0 as? EffortFormError, .notAWholeNumber("reps")) }
        var rpe = EffortForm.field(name: "rpe", target: nil)
        rpe.text = "11"
        XCTAssertThrowsError(try EffortForm.metrics(from: [rpe])) { XCTAssertEqual($0 as? EffortFormError, .outOfRange("rpe")) }
        var tempo = EffortForm.field(name: "tempo", target: nil)
        tempo.text = "slow"
        XCTAssertThrowsError(try EffortForm.metrics(from: [tempo])) { XCTAssertEqual($0 as? EffortFormError, .badFormat("tempo")) }
        var load = EffortForm.field(name: "load", target: nil)
        load.text = "heavy"
        XCTAssertThrowsError(try EffortForm.metrics(from: [load])) { XCTAssertEqual($0 as? EffortFormError, .notANumber("load")) }
    }
}

final class ClockAndHealthTests: XCTestCase {
    func testUUID7IsOrderedAndWellFormed() throws {
        let pattern = try Fixtures.pattern("uuid")
        let date = Date()
        let ids = (0..<5000).map { _ in UUID7.make(at: date) }
        XCTAssertEqual(ids, ids.sorted())
        XCTAssertEqual(Set(ids).count, ids.count)
        for id in ids.prefix(50) {
            XCTAssertTrue(matches(id, pattern), id)
            XCTAssertEqual(Array(id)[14], "7")
            XCTAssertTrue("89ab".contains(Array(id)[19]), id)
        }
    }

    func testTimestampsMatchTheContract() throws {
        let pattern = try Fixtures.pattern("timestamp")
        let at = Date(timeIntervalSince1970: 1_795_000_000.25)
        let local = Timestamp.string(at, zone: TimeZone(identifier: "America/New_York")!)
        XCTAssertEqual(local, "2026-11-18T06:06:40.250-05:00", "the local offset, so the day reads as it was lived")
        XCTAssertTrue(Timestamp.string(at, zone: TimeZone(identifier: "UTC")!).hasSuffix("Z"))
        for text in [local, Timestamp.string(at)] {
            XCTAssertTrue(matches(text, pattern), text)
            XCTAssertEqual(Timestamp.date(text)?.timeIntervalSince1970 ?? 0, 1_795_000_000.25, accuracy: 0.001)
        }
        XCTAssertEqual(Timestamp.date("2026-11-23T07:42:10-05:00")?.timeIntervalSince1970, 1_795_437_730)
        XCTAssertTrue(matches(LocalDate.today(), try Fixtures.pattern("date")))
        XCTAssertEqual(Timestamp.zone(TimeZone(identifier: "America/New_York")!), "America/New_York")
        XCTAssertEqual(Timestamp.zone(TimeZone(identifier: "UTC")!), "UTC")
        XCTAssertNil(Timestamp.zone(TimeZone(secondsFromGMT: 3600)!), "an offset is not a zone")
    }

    func testATypedActivityIsOneYouHaveOrYoursUnderX() {
        let known = [Activity(id: "barbell_back_squat", name: "Barbell Back Squat"), Activity(id: "x.cable_fly", name: "Cable Fly")]
        XCTAssertEqual(ActivityId.make("Barbell back squat", known: known), "barbell_back_squat")
        XCTAssertEqual(ActivityId.make("barbell_back_squat", known: known), "barbell_back_squat")
        XCTAssertEqual(ActivityId.make("cable fly", known: known), "x.cable_fly")
        XCTAssertEqual(ActivityId.make("Pull Up", known: known), "x.pull_up")
        XCTAssertEqual(ActivityId.make("  ", known: known), "")
    }

    func testContractDatesAreGregorianWhateverCalendarThePhoneShows() throws {
        XCTAssertEqual(Calendar.contract.identifier, .gregorian)
        XCTAssertEqual(Calendar.contract.timeZone, .current)
        var buddhist = Calendar(identifier: .buddhist)
        buddhist.timeZone = .current
        let noon = try XCTUnwrap(buddhist.date(from: DateComponents(year: 2569, month: 11, day: 20, hour: 12)))
        XCTAssertEqual(LocalDate.string(noon), "2026-11-20")
        XCTAssertEqual(LocalDate.date("2026-11-20"), noon)
        let total = DailyTotal(type: "HKQuantityTypeIdentifierStepCount", start: noon, end: noon + 3600, value: 10)
        XCTAssertEqual(HealthMapping.sample(total, readAt: noon)?.sourceId,
                       "daily:HKQuantityTypeIdentifierStepCount:2026-11-20")
    }

    let origin = HealthOrigin(sourceName: "Example Scale", sourceBundleId: "com.example.scale",
                              deviceManufacturer: "Example")

    func reading(_ value: HealthValue, minutes: Double = 0) -> HealthReading {
        HealthReading(uuid: UUID(uuidString: "6F1D0C8E-3A1B-4C2D-9E8F-7A6B5C4D3E2F")!,
                      start: Date(timeIntervalSince1970: 1_795_000_000),
                      end: Date(timeIntervalSince1970: 1_795_000_000 + minutes * 60), value: value, origin: origin)
    }

    func testBodyMassBecomesASample() throws {
        let sample = try XCTUnwrap(HealthMapping.sample(reading(.quantity("HKQuantityTypeIdentifierBodyMass", 82.40000001))))
        XCTAssertEqual(sample.source, "apple_health")
        XCTAssertEqual(sample.sourceId, "6f1d0c8e-3a1b-4c2d-9e8f-7a6b5c4d3e2f")
        XCTAssertEqual(sample.type, "HKQuantityTypeIdentifierBodyMass")
        XCTAssertEqual(sample.value, .number(82.4))
        XCTAssertEqual(sample.unit, "kg")
        XCTAssertEqual(sample.device, "Example Scale")
        XCTAssertEqual(sample.originApp, "com.example.scale", "the app that wrote it, which Health can't tell later")
        XCTAssertNil(sample.raw)
        XCTAssertTrue(matches(sample.start, try Fixtures.pattern("timestamp")))
        XCTAssertNil(HealthMapping.sample(reading(.quantity("HKQuantityTypeIdentifierBodyMass", 0))))
        XCTAssertNil(HealthMapping.sample(reading(.quantity("HKQuantityTypeIdentifierBodyMass", 701))))
    }

    func testEveryHealthKitTypeIsSentInItsCodesOwnUnit() throws {
        for (type, code) in MetricRegistry.healthkit {
            let spec = try XCTUnwrap(MetricRegistry.spec(code))
            XCTAssertEqual(HealthMapping.unit(type), spec.units.first, type)
        }
        XCTAssertEqual(HealthMapping.unit("HKQuantityTypeIdentifierHeartRate"), "/min")
        XCTAssertEqual(HealthMapping.unit("HKQuantityTypeIdentifierBodyFatPercentage"), "%")
        XCTAssertNil(HealthMapping.unit("HKQuantityTypeIdentifierStepCount"), "a count has no unit")
        XCTAssertNotNil(MetricRegistry.healthkit[HealthMapping.sleepType])
    }

    func testHealthReadingsAndWorkouts() throws {
        let fat = try XCTUnwrap(HealthMapping.sample(reading(.quantity("HKQuantityTypeIdentifierBodyFatPercentage", 18.4))))
        XCTAssertEqual(fat.unit, "%")
        XCTAssertNil(HealthMapping.sample(reading(.quantity("HKQuantityTypeIdentifierBodyFatPercentage", 1840))))
        XCTAssertEqual(HealthMapping.sample(reading(.quantity("HKQuantityTypeIdentifierVO2Max", 48.1)))?.unit,
                       "mL/(kg.min)")
        XCTAssertNil(HealthMapping.sample(reading(.quantity("HKQuantityTypeIdentifierUVExposure", 3))),
                     "not read from Apple Health")

        let sleep = try XCTUnwrap(HealthMapping.sample(reading(.sleep(4), minutes: 45)))
        XCTAssertEqual(sleep.type, "HKCategoryTypeIdentifierSleepAnalysis")
        XCTAssertEqual(sleep.value, .string("asleep_deep"))
        XCTAssertNil(sleep.unit)
        XCTAssertNil(HealthMapping.sample(reading(.sleep(99))))

        let totals = WorkoutTotals(durationS: 2700, energyKcal: 480.25, heartRateAvgBpm: 141, heartRateMaxBpm: 0)
        let workout = try XCTUnwrap(HealthMapping.sample(reading(.workout("traditionalStrengthTraining", totals), minutes: 45)))
        XCTAssertEqual(workout.type, "HKWorkoutTypeIdentifier")
        XCTAssertEqual(workout.value, .string("traditionalStrengthTraining"))
        XCTAssertNil(workout.unit)
        XCTAssertEqual(workout.raw, .object(["duration_s": .number(2700), "energy_kcal": .number(480.3),
                                             "heart_rate_avg_bpm": .number(141)]))
        XCTAssertNil(HealthMapping.sample(reading(.workout("Running", totals))))

        let gone = HealthMapping.deletion(UUID(uuidString: "6F1D0C8E-3A1B-4C2D-9E8F-7A6B5C4D3E2F")!,
                                          type: "HKQuantityTypeIdentifierBodyMass")
        XCTAssertEqual(gone, Deletion(source: "apple_health", sourceId: "6f1d0c8e-3a1b-4c2d-9e8f-7a6b5c4d3e2f",
                                      type: "HKQuantityTypeIdentifierBodyMass"))
    }

    func testDailyTotalsAreKeyedOnTheirLocalDayAndRevisedAsItGoes() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        let start = try XCTUnwrap(calendar.date(from: DateComponents(year: 2026, month: 11, day: 20)))
        let end = try XCTUnwrap(calendar.date(byAdding: .day, value: 1, to: start))
        let read = start.addingTimeInterval(12 * 3600)
        let stepType = "HKQuantityTypeIdentifierStepCount"
        let steps = try XCTUnwrap(HealthMapping.sample(
            DailyTotal(type: stepType, start: start, end: end, value: 9412), readAt: read, calendar: calendar))
        XCTAssertEqual(steps.sourceId, "daily:HKQuantityTypeIdentifierStepCount:2026-11-20")
        XCTAssertEqual(steps.value, .number(9412))
        XCTAssertNil(steps.unit)
        XCTAssertNil(steps.device)
        XCTAssertEqual(steps.raw, .object(["modified_at": .string(Timestamp.string(read))]))
        let later = try XCTUnwrap(HealthMapping.sample(
            DailyTotal(type: stepType, start: start, end: end, value: 10_020), readAt: read + 3600, calendar: calendar))
        XCTAssertEqual(later.sourceId, steps.sourceId, "the same day, read again")
        XCTAssertNotEqual(later.raw, steps.raw)

        let heart = try XCTUnwrap(HealthMapping.sample(
            DailyTotal(type: "HKQuantityTypeIdentifierHeartRate", start: start, end: end, value: 71.44, lowest: 48.2,
                       highest: 162), readAt: read, calendar: calendar))
        XCTAssertEqual(heart.unit, "/min")
        XCTAssertEqual(heart.raw, .object(["modified_at": .string(Timestamp.string(read)), "min_bpm": .number(48),
                                           "max_bpm": .number(162)]))
        XCTAssertNil(HealthMapping.sample(DailyTotal(type: stepType, start: start, end: end, value: 0), readAt: read))
    }
}
