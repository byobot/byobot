import Foundation
import XCTest
@testable import ByobotKit

final class WorkoutTests: XCTestCase {
    func fixtureLibrary() throws -> [Activity] {
        guard case .object(let doc) = try Fixtures.json("schema/fixtures/v0/library.json"), let items = doc["activities"] else {
            throw XCTSkip("no activities")
        }
        return try JSONDecoder().decode([Activity].self, from: Fixtures.data(items))
    }

    /// The fixture plan window, with the imported library as its activities.
    func window() throws -> PlanWindow {
        let cases = try Fixtures.cases("schema/fixtures/v0/sync.json")
        let data = try XCTUnwrap(cases.first { $0.string("name") == "sync/plan-window" }?["data"])
        var window = try JSONDecoder().decode(PlanWindow.self, from: Fixtures.data(data))
        let library = try fixtureLibrary()
        window.activities = library + window.activities.filter { a in !library.contains { $0.id == a.id } }
        return window
    }

    func session(_ id: String) throws -> PlannedSession {
        try XCTUnwrap(try window().sessions.first { $0.id == id })
    }

    func makeBook() throws -> Logbook {
        final class Clock: @unchecked Sendable {
            var now = Date(timeIntervalSince1970: 1_795_000_000)
        }
        let clock = Clock()
        var book = Logbook(state: AppState(deviceId: "0193a0b0-0000-7000-8000-00000000ab01"), clock: {
            clock.now += 1
            return clock.now
        })
        book.applyPlan(try window())
        return book
    }

    // MARK: Library and swaps

    func testTheLibraryComesWithThePlan() throws {
        let book = try makeBook()
        XCTAssertEqual(book.library.first { $0.id == "pull_up" }?.targets?.main, ["lats"])
        XCTAssertEqual(ActivityLibrary.equipment(in: book.library).first, "barbell")
    }

    func testSwapsWorkTheSameMusclesWithWhatYouHave() throws {
        let library = try fixtureLibrary()
        let squat = try XCTUnwrap(library.first { $0.id == "barbell_back_squat" })
        let alternatives = Swaps.alternatives(to: squat, in: library, equipment: ["dumbbell", "bench"])
        XCTAssertEqual(alternatives.prefix(3).map(\.id), ["bulgarian_split_squat", "goblet_squat", "walking_lunge"])
        let firstMissing = try XCTUnwrap(alternatives.firstIndex { !$0.missingEquipment.isEmpty })
        XCTAssertTrue(alternatives[..<firstMissing].allSatisfy { $0.missingEquipment.isEmpty })
        XCTAssertTrue(alternatives[firstMissing...].allSatisfy { !$0.missingEquipment.isEmpty })
        XCTAssertFalse(alternatives.contains { $0.id == "plank" }, "a core drill is no squat")

        let pullUp = try XCTUnwrap(library.first { $0.id == "pull_up" })
        XCTAssertEqual(Swaps.alternatives(to: pullUp, in: library, equipment: ["cable"]).first?.id, "lat_pulldown")
        let run = try XCTUnwrap(library.first { $0.id == "run" })
        XCTAssertEqual(Swaps.alternatives(to: run, in: library).first?.id, "treadmill_run")
        XCTAssertEqual(Swaps.alternatives(to: Activity(id: "x", name: "X"), in: library), [], "no muscles, no guesses")
    }

    func testYourEquipmentRanksTheSwaps() throws {
        var book = try makeBook()
        let pullUp = try XCTUnwrap(try session("sess-upper").blocks.first?.efforts.first)
        XCTAssertNil(book.equipment)
        book.setEquipment(["cable"])
        XCTAssertEqual(book.state.equipment, ["cable"])
        XCTAssertEqual(book.alternatives(for: pullUp).first?.id, "lat_pulldown")
        XCTAssertEqual(book.alternatives(for: PlannedEffort(id: "e", activityId: "nope")), [])
    }

    // MARK: Order and swaps

    func testSlotsFollowThePlanThenYourOrder() throws {
        var book = try makeBook()
        let lower = try session("sess-lower-a")
        var workout = book.workout(lower)
        XCTAssertEqual(workout.slots.map(\.id), ["e-warm:1", "e-squat:1", "e-squat:2", "e-squat:3", "e-amrap:1"])
        XCTAssertEqual(workout.next?.id, "e-warm:1")

        book.setOrder(["e-amrap", "e-warm", "e-squat"], for: lower.id)
        workout = book.workout(lower)
        XCTAssertEqual(workout.exercises.map(\.id), ["e-amrap", "e-warm", "e-squat"])
        XCTAssertEqual(workout.slots.first?.id, "e-amrap:1")
        XCTAssertEqual(workout.changeNotes,
                       "Changed the order: Barbell back squat, Barbell back squat, Barbell back squat.")

        // A superset alternates within each round, and its rest comes after the round's last effort.
        let upper = try book.workout(session("sess-upper"))
        XCTAssertEqual(upper.slots.prefix(4).map(\.id), ["e-pullup:1", "e-dip:1", "e-pullup:2", "e-dip:2"])
        XCTAssertNil(upper.slots[0].restSeconds)
        XCTAssertEqual(upper.slots[1].restSeconds, 90)
        XCTAssertEqual(try book.workout(session("sess-lower-a")).slots.first { $0.id == "e-squat:1" }?.restSeconds, 180)
    }

    func testASwapIsLoggedAgainstThePlanWithItsReason() throws {
        var book = try makeBook()
        let upper = try session("sess-upper")
        book.swap("e-pullup", to: Swap(activityId: "lat_pulldown", reason: "bar taken"), for: upper.id)
        book.startSession(planned: upper)
        var workout = book.workout(upper)
        let slot = try XCTUnwrap(workout.next)
        XCTAssertEqual(slot.activityId, "lat_pulldown")
        XCTAssertEqual(workout.notes(for: slot, nil), "In place of Pull-up: bar taken")

        let metrics = Prefill.asPlanned(slot, previous: nil, lastTime: nil)
        XCTAssertEqual(metrics, ["reps": .exact(6)], "the plan's load was for pull-ups")
        let set = book.logEffort(activityId: slot.activityId, metrics: metrics, plannedEffortId: slot.effort.id,
                                 round: slot.round, notes: workout.notes(for: slot, "felt easy"))
        XCTAssertEqual(set.plannedEffortId, "e-pullup")
        XCTAssertEqual(set.notes, "In place of Pull-up: bar taken. felt easy")
        workout = book.workout(upper)
        XCTAssertEqual(workout.exercises.first?.done, 1)
        XCTAssertEqual(workout.next?.id, "e-dip:1")

        let finished = try XCTUnwrap(book.finishSession(notes: "Good one"))
        XCTAssertEqual(finished.notes, "Good one Swapped Pull-up for Lat pulldown (bar taken).")

        book.swap("e-pullup", to: nil, for: upper.id)
        XCTAssertNil(book.changes(for: upper.id).swaps)
    }

    func testUpNextSkipsWhatYouFinished() throws {
        var book = try makeBook()
        XCTAssertEqual(book.upNext(today: "2026-11-23")?.id, "sess-lower-a")
        XCTAssertEqual(book.upNext(today: "2026-11-24")?.id, "sess-mobility")
        let lower = try session("sess-lower-a")
        book.startSession(planned: lower)
        book.finishSession()
        XCTAssertEqual(book.upNext(today: "2026-11-23")?.id, "sess-mobility")
        XCTAssertNil(book.upNext(today: "2027-03-01"))
    }

    // MARK: Logging

    func testDoneAsPlanned() throws {
        let book = try makeBook()
        let lower = book.workout(try session("sess-lower-a"))
        let squat = lower.slots[1]
        XCTAssertEqual(Prefill.asPlanned(squat, previous: nil, lastTime: nil),
                       ["reps": .exact(3), "load": .exact(100, unit: "kg"), "rpe": .exact(8), "tempo": .text("3010")])
        let lastRound = PerformedEffort(id: "p", activityId: "barbell_back_squat", performedAt: "2026-11-23T07:40:00Z",
                                        metrics: ["reps": .exact(5), "load": .exact(100, unit: "kg")])
        XCTAssertEqual(Prefill.asPlanned(squat, previous: lastRound, lastTime: nil)["reps"], .exact(5),
                       "5 is inside 3–5, so it carries over")
        let tooMany = PerformedEffort(id: "p", activityId: "barbell_back_squat", performedAt: "2026-11-23T07:40:00Z",
                                      metrics: ["reps": .exact(7)])
        XCTAssertEqual(Prefill.asPlanned(squat, previous: tooMany, lastTime: nil)["reps"], .exact(3))
        XCTAssertNil(Prefill.asPlanned(lower.slots[0], previous: nil, lastTime: nil)["rest"])
        var logged = book
        logged.startSession(planned: try session("sess-lower-a"))
        logged.logEffort(activityId: "barbell_back_squat", metrics: ["reps": .exact(4), "load": .exact(100, unit: "kg")],
                         plannedEffortId: "e-squat", round: 1)
        let now = logged.workout(try session("sess-lower-a"))
        let second = try XCTUnwrap(now.slots.first { $0.id == "e-squat:2" })
        XCTAssertEqual(now.previous(second)?.round, 1)
        XCTAssertEqual(logged.asPlanned(second, in: now)["reps"], .exact(4))
    }

    func testNudges() {
        var metrics: Metrics = ["reps": .exact(5), "load": .exact(100, unit: "kg"), "rpe": .exact(9.5), "tempo": .text("3010")]
        metrics = Nudge.apply(metrics, "reps", -1)
        metrics = Nudge.apply(metrics, "load", 1)
        metrics = Nudge.apply(metrics, "rpe", 1)
        metrics = Nudge.apply(metrics, "rpe", 1)
        metrics = Nudge.apply(metrics, "tempo", 1)
        XCTAssertEqual(metrics, ["reps": .exact(4), "load": .exact(102.5, unit: "kg"), "rpe": .exact(10), "tempo": .text("3010")])
        XCTAssertEqual(Nudge.apply(["load": .exact(225, unit: "lb")], "load", 1), ["load": .exact(230, unit: "lb")])
        XCTAssertEqual(Nudge.apply(["reps": .exact(0)], "reps", -1), ["reps": .exact(0)])
        XCTAssertEqual(Nudge.apply(["duration": .exact(60, unit: "s")], "duration", 1), ["duration": .exact(65, unit: "s")])
    }

    func testLastTimeIsYourMostRecentOtherSession() throws {
        var book = try makeBook()
        book.startSession(name: "Today")
        book.logEffort(activityId: "barbell_back_squat", metrics: ["reps": .exact(3)])
        let today = try XCTUnwrap(book.state.activeSessionId)
        let last = book.lastTime("barbell_back_squat", excludingSession: today, at: nil)
        XCTAssertEqual(last.efforts.map(\.id), ["eff-1"], "the laptop's set from Monday")
        XCTAssertEqual(book.lastTime("barbell_back_squat", at: nil).efforts.count, 1)
        XCTAssertEqual(book.lastTime("plank", at: nil), .empty)
    }

    // MARK: Locations

    func testAddingLocations() throws {
        var book = try makeBook()
        XCTAssertEqual(book.locations, [])
        let home = book.addLocation(name: " Home Gym ")
        XCTAssertEqual(home, Location(id: "home-gym", name: "Home Gym"))
        XCTAssertEqual(book.currentLocationId, "home-gym")
        XCTAssertEqual(book.addLocation(name: "home gym!").id, "home-gym-2")
        XCTAssertEqual(book.addLocation(name: "Éxample").id, "xample")
        XCTAssertEqual(book.addLocation(name: "🏋️").id, "gym")
        var window = try window()
        window.locations = [Location(id: "home-gym", name: "Garage")]
        book.applyPlan(window)
        XCTAssertEqual(book.location("home-gym")?.name, "Home Gym", "this phone's newer name until it syncs")
        XCTAssertEqual(book.locations.count, 4)
    }

    func testASessionStartsWherePlannedElseWhereYouLastTrained() throws {
        var book = try makeBook()
        book.setLocation("home")
        XCTAssertEqual(book.startSession(name: "Quick one").locationId, "home")
        book.finishSession()
        var lower = try session("sess-lower-a")
        lower.locationId = "example-gym"
        XCTAssertEqual(book.startSession(planned: lower).locationId, "example-gym")
        XCTAssertEqual(book.state.locationId, "example-gym", "where you are now")
        XCTAssertFalse(book.plannedElsewhere(lower))
        book.setLocation("hotel")
        XCTAssertTrue(book.plannedElsewhere(lower))
    }

    func testMovingGymsMidSessionMovesItsSets() throws {
        var book = try makeBook()
        book.setLocation("home")
        book.startSession(name: "Lift")
        let set = book.logEffort(activityId: "barbell_back_squat", metrics: ["load": .exact(100, unit: "kg")])
        XCTAssertNil(set.locationId, "a set takes its session's location")
        XCTAssertEqual(book.locationId(of: set), "home")
        book.setLocation("hotel")
        XCTAssertEqual(book.activeSession?.locationId, "hotel")
        XCTAssertEqual(book.locationId(of: set), "hotel")
        XCTAssertEqual(book.finishSession()?.locationId, "hotel")
    }

    func testLastTimeWithALoadIsAtTheSameGym() throws {
        var book = try makeBook()
        func effort(_ id: String, _ activity: String, _ day: Int, _ location: String, _ metrics: Metrics) -> PerformedEffort {
            PerformedEffort(id: id, activityId: activity, performedAt: "2026-11-\(day)T08:00:00Z", locationId: location,
                            metrics: metrics, sessionId: "s-\(id)")
        }
        var window = try window()
        window.efforts = [
            effort("squat-home", "barbell_back_squat", 10, "home", ["reps": .exact(5), "load": .exact(100, unit: "kg")]),
            effort("squat-hotel", "barbell_back_squat", 15, "hotel", ["reps": .exact(5), "load": .exact(80, unit: "kg")]),
            effort("plank-home", "plank", 12, "home", ["duration": .exact(45, unit: "s")]),
            effort("plank-hotel", "plank", 16, "hotel", ["duration": .exact(60, unit: "s")]),
        ]
        book.applyPlan(window)
        XCTAssertEqual(book.lastTime("barbell_back_squat", at: "home"),
                       LastTime(efforts: [window.efforts[0]], locationId: "home", elsewhere: false))
        XCTAssertEqual(book.lastTime("barbell_back_squat", at: "new-gym"),
                       LastTime(efforts: [window.efforts[1]], locationId: "hotel", elsewhere: true))
        XCTAssertEqual(book.lastTime("barbell_back_squat", at: nil).efforts, [window.efforts[1]])
        XCTAssertEqual(book.lastTime("plank", at: "home"),
                       LastTime(efforts: [window.efforts[3]], locationId: nil, elsewhere: false),
                       "a plank is a plank anywhere")

        var lower = try session("sess-lower-a")
        lower.locationId = "home"
        book.startSession(planned: lower)
        book.setLocation("new-gym")
        let squat = try XCTUnwrap(book.workout(lower).slots.first { $0.id == "e-squat:1" })
        XCTAssertEqual(book.asPlanned(squat, in: book.workout(lower))["load"], .exact(100, unit: "kg"),
                       "planned for home, and the hotel's 80 kg doesn't carry over to a new gym")
    }

    func testDoneAsPlannedAtAnotherGymStartsFromYourLoadsThere() throws {
        let lower = try makeBook().workout(try session("sess-lower-a"))
        let squat = lower.slots[1]
        let here = PerformedEffort(id: "p", activityId: "barbell_back_squat", performedAt: "2026-11-20T07:40:00Z",
                                   metrics: ["reps": .exact(5), "load": .exact(90, unit: "kg")])
        XCTAssertEqual(Prefill.asPlanned(squat, previous: nil, lastTime: here)["load"], .exact(100, unit: "kg"))
        XCTAssertEqual(Prefill.asPlanned(squat, previous: nil, lastTime: here, atAnotherGym: true)["load"],
                       .exact(90, unit: "kg"))
        XCTAssertEqual(Prefill.asPlanned(squat, previous: nil, lastTime: nil, atAnotherGym: true)["load"],
                       .exact(100, unit: "kg"), "with no history there, the plan is all there is")
    }

    // MARK: Clocks

    func testRestCountdown() {
        let start = Date(timeIntervalSince1970: 0)
        let rest = Countdown(startedAt: start, seconds: 90)
        XCTAssertEqual(rest.remaining(at: start.addingTimeInterval(30)), 60)
        XCTAssertFalse(rest.isDone(at: start.addingTimeInterval(89)))
        XCTAssertTrue(rest.isDone(at: start.addingTimeInterval(90)))
        XCTAssertEqual(rest.adding(30).remaining(at: start.addingTimeInterval(90)), 30)
    }

    func testANorwegianFourByFourRunsOnTheIntervalClock() throws {
        let block = Block(id: "b-4x4", rounds: 4, metrics: ["rest": .exact(3, unit: "min")], efforts: [
            PlannedEffort(id: "e-4x4", activityId: "run", metrics: ["duration": .exact(4, unit: "min"),
                                                                    "heart_rate": Measure(min: 170, max: 180, unit: "bpm")]),
        ])
        let workout = Workout(session: PlannedSession(id: "s", scheduledOn: "2026-11-28", name: "4x4", blocks: [block]))
        XCTAssertTrue(workout.isIntervalBlock(block))
        let clock = try XCTUnwrap(IntervalClock(slots: workout.slots))
        XCTAssertEqual(clock.phases.map(\.kind), [.work, .rest, .work, .rest, .work, .rest, .work])
        XCTAssertEqual(clock.total, 4 * 240 + 3 * 180)
        XCTAssertEqual(clock.position(elapsed: 0)?.index, 0)
        XCTAssertEqual(clock.position(elapsed: 250)?.index, 1)
        XCTAssertEqual(clock.position(elapsed: 250)?.remaining, 170)
        XCTAssertNil(clock.position(elapsed: clock.total))
        XCTAssertEqual(clock.finishedSlots(elapsed: 239).count, 0)
        XCTAssertEqual(clock.finishedSlots(elapsed: 240).map(\.round), [1])
        XCTAssertEqual(clock.finishedSlots(elapsed: clock.total).map(\.round), [1, 2, 3, 4])
        XCTAssertEqual(Prefill.asPlanned(workout.slots[0], previous: nil, lastTime: nil), ["duration": .exact(4, unit: "min")],
                       "your heart rate is read, not made up")
    }

    func testAnEMOMAndHoldsOnEachSide() throws {
        let book = try makeBook()
        let emom = book.workout(try session("sess-emom"))
        XCTAssertTrue(emom.isIntervalBlock(emom.session.blocks[0]))
        let clock = try XCTUnwrap(IntervalClock(slots: emom.slots))
        XCTAssertEqual(clock.phases.count, 10)
        XCTAssertEqual(clock.total, 600)

        // The plan names each side of the couch stretch, so each is its own hold.
        let hips = book.workout(try session("sess-mobility"))
        XCTAssertFalse(hips.slots[0].perSide)
        // A unilateral hold with no side runs left then right, and logs once.
        let pigeon = Block(id: "b", efforts: [PlannedEffort(id: "e", activityId: "pigeon_stretch",
                                                            metrics: ["duration": .exact(45, unit: "s")])])
        let slots = Workout(session: PlannedSession(id: "s", scheduledOn: "2026-11-28", name: "Hips", blocks: [pigeon]),
                            library: try fixtureLibrary()).slots
        XCTAssertTrue(slots[0].perSide)
        let hold = try XCTUnwrap(IntervalClock(slots: slots))
        XCTAssertEqual(hold.phases.map(\.side), [.left, .right])
        XCTAssertEqual(hold.finishedSlots(elapsed: 45).count, 0)
        XCTAssertEqual(hold.finishedSlots(elapsed: 90).count, 1)
        XCTAssertNil(IntervalClock(slots: book.workout(try session("sess-upper")).slots), "reps aren't on a clock")

        XCTAssertEqual(emom.clockSlots(from: emom.slots[0])?.count, 10)
        XCTAssertEqual(hips.clockSlots(from: hips.slots[0])?.map(\.id),
                       ["e-couch-l:1", "e-couch-r:1", "e-couch-l:2", "e-couch-r:2"])
        let upper = book.workout(try session("sess-upper"))
        XCTAssertNil(upper.clockSlots(from: upper.slots[0]))
    }

    // MARK: Summary

    func testSummaryShowsPlannedAgainstDone() throws {
        var book = try makeBook()
        let upper = try session("sess-upper")
        book.startSession(planned: upper)
        for slot in book.workout(upper).slots.prefix(3) {
            book.logEffort(activityId: slot.activityId, metrics: ["reps": .exact(8)], plannedEffortId: slot.effort.id,
                           round: slot.round)
        }
        let extra = book.logEffort(activityId: "face-pull", metrics: ["reps": .exact(15)])
        let performed = try XCTUnwrap(book.finishSession())
        let summary = book.workout(upper).summary(extras: [extra], performed: performed)
        XCTAssertEqual(summary.rows.map(\.exercise.id), ["e-pullup", "e-dip"])
        XCTAssertEqual(summary.rows.map(\.sets.count), [2, 1])
        XCTAssertEqual(summary.rows[0].sets.map(\.round), [1, 2])
        XCTAssertEqual(summary.done, 3)
        XCTAssertEqual(summary.total, 8)
        XCTAssertEqual(summary.extras.map(\.id), [extra.id])
        XCTAssertNotNil(summary.duration)
        XCTAssertEqual(book.finishedSessions.map(\.id), [performed.id])
    }
}
