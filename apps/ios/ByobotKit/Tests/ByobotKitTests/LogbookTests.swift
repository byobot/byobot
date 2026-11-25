import Foundation
import XCTest
@testable import ByobotKit

final class LogbookTests: XCTestCase {
    /// A clock that moves one second per reading, so every event has its own time.
    final class TickingClock: @unchecked Sendable {
        var now = Date(timeIntervalSince1970: 1_795_000_000)
        func read() -> Date {
            now += 1
            return now
        }
    }

    func plannedSession() throws -> PlannedSession {
        let cases = try Fixtures.cases("schema/fixtures/v0/sync.json")
        let window = try XCTUnwrap(cases.first { $0.string("name") == "sync/plan-window" }?["data"])
        let plan = try JSONDecoder().decode(PlanWindow.self, from: Fixtures.data(window))
        return try XCTUnwrap(plan.sessions.first { $0.id == "sess-upper" })
    }

    func logbook() -> Logbook {
        let clock = TickingClock()
        return Logbook(state: AppState(deviceId: "0193a0b0-0000-7000-8000-00000000ab01"), clock: { clock.read() })
    }

    func testASessionWritesContractShapedEvents() throws {
        var book = logbook()
        let planned = try plannedSession()
        let session = book.startSession(planned: planned)
        let pullUp = planned.blocks[0].efforts[0]
        book.logEffort(activityId: pullUp.activityId, metrics: ["reps": .exact(7), "load": .exact(10, unit: "kg")],
                       plannedEffortId: pullUp.id, round: 1)
        let finished = try XCTUnwrap(book.finishSession())

        XCTAssertEqual(finished.id, session.id)
        XCTAssertNotNil(finished.endedAt)
        XCTAssertEqual(finished.plannedSessionId, "sess-upper")
        XCTAssertNil(book.activeSession)
        XCTAssertEqual(book.state.events.map(\.type), [.sessionPerformed, .effortPerformed, .sessionPerformed])
        XCTAssertEqual(book.pendingEvents.map(\.id), book.state.events.map(\.id))

        // Every id and time is in the contract's format, and the JSON uses the contract's names.
        let uuid = try Fixtures.pattern("uuid"), ts = try Fixtures.pattern("timestamp"), id = try Fixtures.pattern("id")
        for event in book.state.events {
            XCTAssertTrue(matches(event.id, uuid), event.id)
            XCTAssertTrue(matches(event.ts, ts), event.ts)
            XCTAssertTrue(matches(event.deviceId, id))
        }
        let request = try JSONValue.parse(JSONEncoder().encode(EventsRequest(events: book.pendingEvents)))
        guard case .object(let body) = request, case .array(let events)? = body["events"],
              case .object(let effortEvent) = events[1], case .object(let payload)? = effortEvent["payload"] else {
            return XCTFail("unexpected shape")
        }
        XCTAssertEqual(effortEvent["type"], .string("effort.performed"))
        XCTAssertEqual(effortEvent["schema_version"], .number(0))
        XCTAssertEqual(effortEvent["actor"], .object(["kind": .string("human"), "id": .string("me")]))
        XCTAssertEqual(payload["planned_effort_id"], .string(pullUp.id))
        XCTAssertEqual(payload["session_id"], .string(session.id))
        XCTAssertEqual(payload["round"], .number(1))
        XCTAssertTrue(matches(payload.string("performed_at") ?? "", ts))
        XCTAssertEqual(payload["metrics"], .object(["reps": .object(["value": .number(7)]),
                                                    "load": .object(["value": .number(10), "unit": .string("kg")])]))
    }

    func testLoggedRoundsMergeThisPhoneAndTheLaptop() throws {
        var book = logbook()
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0], dip = planned.blocks[0].efforts[1]
        // The laptop already has round 1 of the pull-ups (logged elsewhere).
        let remote = PerformedEffort(id: "remote-1", activityId: "pull_up", performedAt: "2026-11-26T07:00:00Z",
                                     plannedEffortId: pullUp.id, round: 1, metrics: ["reps": .exact(8)])
        book.applyPlan(PlanWindow(from: "2026-11-22", to: "2026-12-05", activities: [], plans: [], sessions: [planned],
                                  efforts: [remote]))
        book.startSession(planned: planned)
        book.logEffort(activityId: "pull_up", metrics: ["reps": .exact(6)], plannedEffortId: pullUp.id, round: 2)
        let dip1 = book.logEffort(activityId: "dip", metrics: ["reps": .exact(10)], plannedEffortId: dip.id, round: 1)

        var logged = book.logged(for: planned)
        XCTAssertEqual(logged[pullUp.id]?[1]?.id, "remote-1")
        XCTAssertEqual(logged[pullUp.id]?[2]?.metrics?["reps"], .exact(6))
        XCTAssertEqual(logged[dip.id]?[1]?.id, dip1.id)
        XCTAssertEqual(planned.plannedCount, 8)

        // Undo is an event too, and the effort drops out at once.
        let undo = book.undoEffort(id: dip1.id)
        XCTAssertEqual(undo.map(\.type), [.eventUndone])
        logged = book.logged(for: planned)
        XCTAssertNil(logged[dip.id]?[1])
        XCTAssertEqual(book.undoEffort(id: dip1.id), [], "already undone")
        XCTAssertEqual(book.efforts(inSession: book.state.activeSessionId!).count, 1)
    }

    func testPushResultsClearTheOutbox() throws {
        var book = logbook()
        book.startSession(name: "Easy run")
        let a = book.logEffort(activityId: "run", metrics: ["distance": .exact(5, unit: "km")])
        book.logEffort(activityId: "run", metrics: ["distance": .exact(1, unit: "km")])
        let sent = book.pendingEvents
        XCTAssertEqual(sent.count, 3)
        let response = EventsResponse(results: [
            EventResult(index: 0, id: sent[0].id, status: .accepted),
            EventResult(index: 1, id: sent[1].id, status: .duplicate),
            EventResult(index: 2, id: sent[2].id, status: .rejected, errors: [.object(["message": .string("nope")])]),
        ], accepted: 1, duplicate: 1, rejected: 1)
        // Written while the push was in flight: stays pending.
        book.logEffort(activityId: "run", metrics: [:])
        book.applyPush(response, sent: sent)
        XCTAssertEqual(book.pendingEvents.count, 1)
        XCTAssertEqual(book.state.rejected.map(\.eventId), [sent[2].id])
        XCTAssertEqual(book.localEfforts.first?.id, a.id)
        XCTAssertNil(book.localEfforts.last?.metrics, "no metrics means none, not an empty map")
    }

    func testCorrectingASetKeepsItsTimeAndRound() throws {
        var book = logbook()
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0]
        book.startSession(planned: planned)
        let first = book.logEffort(activityId: "pull_up", metrics: ["reps": .exact(8)], plannedEffortId: pullUp.id, round: 2)
        let fixed = book.editEffort(first, metrics: ["reps": .exact(6)], side: nil, notes: "miscounted")

        XCTAssertEqual(fixed.id, first.id)
        XCTAssertEqual(fixed.performedAt, first.performedAt)
        XCTAssertEqual(fixed.round, 2)
        XCTAssertEqual(fixed.sessionId, first.sessionId)
        XCTAssertEqual(book.localEfforts, [fixed])
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[2], fixed)
        XCTAssertEqual(book.pendingEvents.filter { $0.type == .effortPerformed }.count, 2, "the correction is a new event")
        XCTAssertGreaterThan(book.pendingEvents.last!.ts, book.pendingEvents[1].ts)
        XCTAssertFalse(book.loggedElsewhere(first.id))

        let moved = book.editEffort(fixed, metrics: ["reps": .exact(6)], side: nil, notes: nil,
                                    performedAt: "2026-11-26T07:05:00-05:00")
        XCTAssertEqual(moved.performedAt, "2026-11-26T07:05:00-05:00")
        XCTAssertNil(moved.notes)
    }

    func testCorrectingASetFromAnotherDeviceAndUndoingIt() throws {
        var book = logbook()
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0]
        let remote = PerformedEffort(id: "remote-1", activityId: "pull_up", performedAt: "2026-11-26T07:00:00Z",
                                     plannedEffortId: pullUp.id, round: 1, metrics: ["reps": .exact(8)])
        book.applyPlan(PlanWindow(from: "2026-11-22", to: "2026-12-05", activities: [], plans: [], sessions: [planned],
                                  efforts: [remote]))
        let fixed = book.editEffort(remote, metrics: ["reps": .exact(7)], side: .both, notes: nil)
        XCTAssertEqual(fixed.performedAt, remote.performedAt)
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1], fixed)
        XCTAssertTrue(book.loggedElsewhere(remote.id))
        XCTAssertTrue(book.canUndo(remote.id))

        XCTAssertEqual(book.undoEffort(id: remote.id).count, 1)
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1], remote, "back to the other device's version")
        XCTAssertFalse(book.canUndo(remote.id))
    }

    func testThePulledVersionWinsOnceThePhoneHasSynced() throws {
        var book = logbook()
        let planned = try plannedSession()
        let pullUp = planned.blocks[0].efforts[0]
        let remote = PerformedEffort(id: "remote-1", activityId: "pull_up", performedAt: "2026-11-26T07:00:00Z",
                                     plannedEffortId: pullUp.id, round: 1, metrics: ["reps": .exact(8)])
        func pull(_ efforts: [PerformedEffort]) {
            book.applyPlan(PlanWindow(from: "2026-11-22", to: "2026-12-05", activities: [], plans: [],
                                      sessions: [planned], efforts: efforts))
        }
        pull([remote])
        let mine = book.editEffort(remote, metrics: ["reps": .exact(7)], side: nil, notes: nil)
        pull([remote])
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1], mine, "not pushed yet, so the phone's is newer")

        let sent = book.pendingEvents
        book.applyPush(EventsResponse(results: sent.enumerated().map { EventResult(index: $0.offset, id: $0.element.id, status: .accepted) },
                                      accepted: sent.count, duplicate: 0, rejected: 0), sent: sent)
        var laterElsewhere = remote
        laterElsewhere.metrics = ["reps": .exact(9)]
        pull([laterElsewhere])
        XCTAssertEqual(book.logged(for: planned)[pullUp.id]?[1], laterElsewhere)
    }

    func testUndoRemovesASetLoggedHereWithItsCorrections() throws {
        var book = logbook()
        let planned = try plannedSession()
        let dip = planned.blocks[0].efforts[1]
        book.startSession(planned: planned)
        let first = book.logEffort(activityId: "dip", metrics: ["reps": .exact(10)], plannedEffortId: dip.id, round: 1)
        book.editEffort(first, metrics: ["reps": .exact(9)], side: nil, notes: nil)
        XCTAssertEqual(book.undoEffort(id: first.id).count, 2)
        XCTAssertNil(book.logged(for: planned)[dip.id]?[1])
        XCTAssertEqual(book.localEfforts, [])
        XCTAssertNil(book.state.remoteOriginals)
    }

    func testStateSurvivesSaving() throws {
        var book = logbook()
        book.startSession(name: "Mobility")
        book.logEffort(activityId: "couch_stretch", metrics: ["duration": .exact(90, unit: "s")], side: .left)
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
        XCTAssertEqual(fields[1].units, ["kg", "lb", "%1rm"])
        XCTAssertNil(fields[2].target)
    }

    func testCorrectingStartsFromWhatWasLogged() {
        let planned = PlannedEffort(id: "e", activityId: "barbell_back_squat", metrics: ["load": .exact(100, unit: "kg")])
        let activity = Activity(id: "barbell_back_squat", name: "Squat", defaultMetrics: ["reps", "load", "rpe"])
        let fields = EffortForm.fields(planned: planned, activity: activity,
                                       filledFrom: ["reps": .exact(5), "load": .exact(225, unit: "lb"), "x.grade": .text("V4")])
        XCTAssertEqual(fields.map(\.name), ["load", "reps", "rpe", "x.grade"])
        XCTAssertEqual(fields.map(\.text), ["225", "5", "", "V4"])
        XCTAssertEqual(fields[0].unit, "lb")
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
        fields[1].unit = "lb"
        fields[3].text = "3010"
        fields[4].text = "V4"
        fields[5].text = "12,5"
        let metrics = try EffortForm.metrics(from: fields)
        XCTAssertEqual(metrics, ["reps": .exact(5), "load": .exact(225, unit: "lb"), "tempo": .text("3010"),
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
        let text = Timestamp.string(Date(timeIntervalSince1970: 1_795_000_000.25))
        XCTAssertTrue(matches(text, pattern), text)
        XCTAssertTrue(text.hasSuffix("Z"))
        XCTAssertEqual(Timestamp.date(text)?.timeIntervalSince1970 ?? 0, 1_795_000_000.25, accuracy: 0.001)
        XCTAssertEqual(Timestamp.date("2026-11-23T07:42:10-05:00")?.timeIntervalSince1970, 1_795_437_730)
        let date = try Fixtures.pattern("date")
        XCTAssertTrue(matches(LocalDate.today(), date))
    }

    func testBodyMassBecomesTelemetry() throws {
        let origin = HealthOrigin(sourceName: "Example Scale", sourceBundleId: "com.example.scale",
                                  deviceManufacturer: "Example")
        let reading = HealthReading(uuid: UUID(uuidString: "6F1D0C8E-3A1B-4C2D-9E8F-7A6B5C4D3E2F")!,
                                    start: Date(timeIntervalSince1970: 1_795_000_000),
                                    end: Date(timeIntervalSince1970: 1_795_000_000),
                                    value: .quantity(.bodyMass, 82.40000001), origin: origin)
        let sample = try XCTUnwrap(HealthMapping.sample(reading))
        XCTAssertEqual(sample.source, "apple-health")
        XCTAssertEqual(sample.sourceId, "6f1d0c8e-3a1b-4c2d-9e8f-7a6b5c4d3e2f")
        XCTAssertEqual(sample.type, .bodyMass)
        XCTAssertEqual(sample.value, .number(82.4))
        XCTAssertEqual(sample.unit, "kg")
        XCTAssertEqual(sample.device, TelemetryDevice(id: "com.example.scale", name: "Example Scale",
                                                      manufacturer: "Example"))
        let ts = try Fixtures.pattern("timestamp")
        XCTAssertTrue(matches(sample.start, ts))
        var bad = reading
        bad.value = .quantity(.bodyMass, 0)
        XCTAssertNil(HealthMapping.sample(bad))
    }

    func testHealthReadingsTakeTheContractsUnits() throws {
        let origin = HealthOrigin(sourceName: "Example Watch", sourceBundleId: "com.example.watch")
        func reading(_ value: HealthValue) -> HealthReading {
            HealthReading(uuid: UUID(), start: Date(timeIntervalSince1970: 1_795_000_000),
                          end: Date(timeIntervalSince1970: 1_795_002_700), value: value, origin: origin)
        }
        let fat = try XCTUnwrap(HealthMapping.sample(reading(.quantity(.bodyFatPercentage, 18.4))))
        XCTAssertEqual(fat.unit, "%")
        XCTAssertNil(HealthMapping.sample(reading(.quantity(.bodyFatPercentage, 1840))))
        XCTAssertEqual(HealthMapping.sample(reading(.quantity(.vo2Max, 48.1)))?.unit, "ml/kg*min")
        XCTAssertNil(HealthMapping.sample(reading(.quantity(.strain, 10))), "not read from Apple Health")

        let sleep = try XCTUnwrap(HealthMapping.sample(reading(.sleep(4))))
        XCTAssertEqual(sleep.type, .sleepAnalysis)
        XCTAssertEqual(sleep.value, .string("asleepDeep"))
        XCTAssertNil(sleep.unit)
        XCTAssertNil(HealthMapping.sample(reading(.sleep(99))))

        let totals = WorkoutTotals(durationS: 2700, energyKcal: 480.25, heartRateAvgBpm: 141, heartRateMaxBpm: 0)
        let workout = try XCTUnwrap(HealthMapping.sample(reading(.workout("traditionalStrengthTraining", totals))))
        XCTAssertEqual(workout.type, .workout)
        XCTAssertEqual(workout.value, .string("traditionalStrengthTraining"))
        XCTAssertNil(workout.unit)
        XCTAssertEqual(workout.raw, .object(["duration_s": .number(2700), "energy_kcal": .number(480.3),
                                             "heart_rate_avg_bpm": .number(141)]))
        XCTAssertNil(HealthMapping.sample(reading(.workout("Running", totals))))
    }
}
