// Sample data to look around the app with, such as for App Review, with no laptop or Drive. Nothing syncs.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

public enum Demo {
    /// A plan window around `today` (`YYYY-MM-DD`): a plan with a session today and one in two days, and a
    /// session three days ago at `example_gym` for history and last time. Its activities are the shipped library's.
    public static func window(today: String, calendar: Calendar = .contract) -> PlanWindow {
        let noon = LocalDate.date(today, calendar: calendar) ?? Date()
        func day(_ offset: Int) -> String { LocalDate.adding(days: offset, to: noon, calendar: calendar) }
        func at(_ offset: Int, minutes: Int) -> String {
            let date = calendar.date(byAdding: .day, value: offset, to: noon) ?? noon
            return Timestamp.string(date.addingTimeInterval(Double(minutes * 60)))
        }
        var count = 0
        func entry(_ id: String, _ payload: [String: Any]) -> [String: Any] {
            count += 1
            return ["id": id, "payload": payload, "prev": [String(format: "0193a0b0-0000-7000-8000-%012d", count)]]
        }
        func measure(_ value: Double, _ unit: String? = nil) -> [String: Any] {
            var measure: [String: Any] = ["value": value]
            if let unit { measure["unit"] = unit }
            return measure
        }
        func target(_ id: String, _ activity: String, reps: Double, load: Double?) -> [String: Any] {
            var metrics = ["reps": measure(reps)]
            if let load { metrics["load"] = measure(load, "kg") }
            return ["id": id, "activity_id": activity, "metrics": metrics]
        }
        func block(_ id: String, rounds: Int, _ efforts: [[String: Any]]) -> [String: Any] {
            ["id": id, "rounds": rounds, "metrics": ["rest": measure(2, "min")], "efforts": efforts]
        }
        let plan = "demo_plan"
        let partOfPlan: [String: Any] = ["part_of": ["kind": "plan", "id": plan]]
        let past = "demo_done_lower"
        let partOfPast: [String: Any] = ["part_of": ["kind": "session", "id": past]]
        let lifts: [(String, Double, Double)] = [("barbell_back_squat", 5, 77.5), ("barbell_back_squat", 5, 77.5),
                                                 ("barbell_romanian_deadlift", 8, 57.5)]
        let lower: [[String: Any]] = [
            block("demo_lower_warmup", rounds: 1, [target("demo_lower_goblet", "goblet_squat", reps: 10, load: 16)]),
            block("demo_lower_main", rounds: 3, [target("demo_lower_squat", "barbell_back_squat", reps: 5, load: 80)]),
            block("demo_lower_more", rounds: 3, [
                target("demo_lower_rdl", "barbell_romanian_deadlift", reps: 8, load: 60),
                target("demo_lower_lunge", "walking_lunge", reps: 10, load: 12),
            ]),
        ]
        let upper: [[String: Any]] = [
            block("demo_upper_main", rounds: 3, [
                target("demo_upper_press", "dumbbell_bench_press", reps: 8, load: 22),
                target("demo_upper_pull", "pull_up_overhand_grip", reps: 6, load: nil),
            ]),
        ]
        let planned: [[String: Any]] = [
            entry("demo_lower", ["scheduled_on": day(0), "name": "Lower body", "links": partOfPlan, "blocks": lower]),
            entry("demo_upper", ["scheduled_on": day(2), "name": "Upper body", "links": partOfPlan, "blocks": upper]),
        ]
        let time: [String: Any] = ["start": at(-3, minutes: -240), "end": at(-3, minutes: -190)]
        let sessions: [[String: Any]] = [
            entry(past, ["name": "Lower body", "location_id": "example_gym",
                         "status": "completed", "time": time]),
        ]
        var efforts: [[String: Any]] = []
        for (n, lift) in lifts.enumerated() {
            let payload: [String: Any] = [
                "activity_id": lift.0, "location_id": "example_gym", "round": n == 1 ? 2 : 1,
                "status": "completed", "links": partOfPast,
                "time": ["start": at(-3, minutes: -230 + n * 5)],
                "metrics": ["reps": measure(lift.1), "load": measure(lift.2, "kg")],
            ]
            efforts.append(entry("demo_done_\(n + 1)", payload))
        }
        var window: [String: Any] = ["from": day(-7), "to": day(14), "since": day(-7),
                                     "store_id": "0193a0b0-0000-7000-8000-000000000000", "hlc": "0000000000000-000000"]
        window["locations"] = [entry("example_gym", ["name": "Example Gym"])]
        window["plans"] = [entry(plan, ["name": "Example strength block", "starts_on": day(-7), "ends_on": day(49)])]
        window["planned_sessions"] = planned
        window["sessions"] = sessions
        window["efforts"] = efforts
        for key in ["activities", "settings", "removed", "erased", "rejected"] { window[key] = [Any]() }
        window["received"] = [String: String]()
        window["kind_versions"] = KindVersion.all
        guard let data = try? JSONSerialization.data(withJSONObject: window),
              let decoded = try? JSONDecoder().decode(PlanWindow.self, from: data) else {
            return PlanWindow(from: today, to: today)
        }
        return decoded
    }
}
