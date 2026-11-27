// Done-as-planned values, +/- nudges, rest and interval clocks, last time, and the session summary.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

public enum Prefill {
    /// Targets you read off a sensor rather than do, so "done as planned" doesn't make them up.
    static let measured: Set<String> = ["rest", "heart_rate"]

    /// What "done as planned" logs: an exact target as is, a range's bottom unless the last round already
    /// fell inside it. A swap keeps the plan's reps and time but not its load, which may not carry over; nor
    /// does a load planned for another gym, so there the last round or last time here wins over the plan.
    public static func asPlanned(_ slot: Slot, previous: PerformedEffort?, lastTime: PerformedEffort?,
                                 atAnotherGym: Bool = false) -> Metrics {
        let previous = previous?.activityId == slot.activityId ? previous : nil
        var out: Metrics = [:]
        for (name, target) in slot.effort.metrics ?? [:] where !measured.contains(name) {
            if slot.swap != nil, name == "load" { continue }
            if let last = previous?.metrics?[name], fits(last, target) {
                out[name] = last
            } else if let value = target.value {
                out[name] = Measure(value: value, unit: target.unit)
            } else if let n = target.min ?? target.max {
                out[name] = Measure(value: .number(n), unit: target.unit)
            }
        }
        if slot.swap != nil || atAnotherGym, let load = (previous ?? lastTime)?.metrics?["load"] {
            out["load"] = load
        }
        return out
    }

    static func fits(_ measure: Measure, _ target: Measure) -> Bool {
        guard measure.unit == target.unit else { return false }
        if let value = target.value { return measure.value == value }
        guard let n = measure.value?.number else { return false }
        return (target.min.map { n >= $0 } ?? true) && (target.max.map { n <= $0 } ?? true)
    }
}

public enum Nudge {
    public static func step(_ metric: String, unit: String?) -> Double? {
        switch (metric, unit) {
        case ("reps", _), ("rir", _), ("heart_rate", _): return 1
        case ("load", "[lb_av]"): return 5
        case ("load", _): return 2.5
        case ("rpe", _), ("incline", _): return 0.5
        case ("duration", "min"), ("rest", "min"): return 0.5
        case ("duration", "h"): return 0.25
        case ("duration", _), ("rest", _): return 5
        case ("distance", "m"), ("distance", "[yd_i]"), ("distance", "[ft_i]"): return 50
        case ("distance", _): return 0.1
        case ("power", _): return 5
        case ("pace", _): return 5
        default: return nil
        }
    }

    /// One step up (`+1`) or down (`-1`), kept within the metric's bounds.
    public static func apply(_ metrics: Metrics, _ name: String, _ direction: Int) -> Metrics {
        guard var measure = metrics[name], let n = measure.value?.number, let step = step(name, unit: measure.unit) else {
            return metrics
        }
        var next = ((n + Double(direction) * step) * 1000).rounded() / 1000
        next = max(MetricRegistry.spec(name)?.minimum ?? 0, next)
        if MetricRegistry.spec(name)?.exclusiveMinimum == true, next <= 0 { return metrics }
        if let top = MetricRegistry.spec(name)?.maximum { next = min(top, next) }
        measure.value = .number(next)
        var out = metrics
        out[name] = measure
        return out
    }
}

public struct Countdown: Codable, Equatable, Sendable {
    public var startedAt: Date
    public var seconds: TimeInterval

    public init(startedAt: Date, seconds: TimeInterval) {
        self.startedAt = startedAt
        self.seconds = seconds
    }

    public var endsAt: Date { startedAt.addingTimeInterval(seconds) }
    public func remaining(at now: Date) -> TimeInterval { max(0, endsAt.timeIntervalSince(now)) }
    public func isDone(at now: Date) -> Bool { remaining(at: now) <= 0 }
    public func adding(_ more: TimeInterval) -> Countdown {
        Countdown(startedAt: startedAt, seconds: max(0, seconds + more))
    }
}

/// Work and rest phases for timed slots: a hold, each side of a hold, or a whole interval block.
public struct IntervalClock: Equatable, Sendable {
    public struct Phase: Equatable, Sendable {
        public enum Kind: Equatable, Sendable { case work, rest }
        public var kind: Kind
        public var seconds: TimeInterval
        public var slot: Slot
        public var side: Side?
        /// The slot's last work phase, after which it is logged.
        public var finishesSlot: Bool
    }

    public var phases: [Phase]

    /// Nil unless every slot has a time.
    public init?(slots: [Slot]) {
        var phases: [Phase] = []
        for (i, slot) in slots.enumerated() {
            guard let work = slot.workSeconds, work > 0 else { return nil }
            let sides: [Side?] = slot.perSide ? [.left, .right] : [nil]
            for (j, side) in sides.enumerated() {
                phases.append(Phase(kind: .work, seconds: work, slot: slot, side: side, finishesSlot: j == sides.count - 1))
            }
            if i < slots.count - 1, let rest = slot.restSeconds, rest > 0 {
                phases.append(Phase(kind: .rest, seconds: rest, slot: slot, side: nil, finishesSlot: false))
            }
        }
        guard !phases.isEmpty else { return nil }
        self.phases = phases
    }

    public var total: TimeInterval { phases.reduce(0) { $0 + $1.seconds } }

    /// Where the clock is after `elapsed` seconds; nil once it has run out.
    public func position(elapsed: TimeInterval) -> (index: Int, remaining: TimeInterval)? {
        var start: TimeInterval = 0
        for (i, phase) in phases.enumerated() {
            if elapsed < start + phase.seconds { return (i, start + phase.seconds - elapsed) }
            start += phase.seconds
        }
        return nil
    }

    /// Slots whose work has finished by `elapsed`, in order.
    public func finishedSlots(elapsed: TimeInterval) -> [Slot] {
        var start: TimeInterval = 0
        var out: [Slot] = []
        for phase in phases {
            start += phase.seconds
            if start > elapsed { break }
            if phase.kind == .work, phase.finishesSlot { out.append(phase.slot) }
        }
        return out
    }
}

public struct SummaryRow: Identifiable, Equatable, Sendable {
    public var exercise: Exercise
    public var sets: [PerformedEffort]
    public var id: String { exercise.id }
}

public struct SessionSummary: Equatable, Sendable, Identifiable {
    public var rows: [SummaryRow]
    public var extras: [PerformedEffort]
    public var done: Int
    public var total: Int
    public var duration: TimeInterval?
    public var id: String { rows.map(\.id).joined(separator: ",") + "\(done)" }
}

extension Workout {
    public func summary(extras: [PerformedEffort] = [], performed: PerformedSession? = nil) -> SessionSummary {
        let rows = exercises.map { exercise in
            SummaryRow(exercise: exercise, sets: (logged[exercise.effort.id] ?? [:]).sorted { $0.key < $1.key }.map(\.value))
        }
        let duration = performed.flatMap { s in
            s.endedAt.flatMap(Timestamp.date).flatMap { end in Timestamp.date(s.startedAt).map { end.timeIntervalSince($0) } }
        }
        return SessionSummary(rows: rows, extras: extras, done: done, total: total, duration: duration)
    }
}

extension Logbook {
    /// The activities your laptop sent, with their muscles and equipment.
    public var library: [Activity] { state.plan?.activities ?? [] }

    /// Every set of an activity this phone knows of: pulled from the laptop, with this phone's own versions on top.
    func efforts(of activityId: String) -> [PerformedEffort] {
        currentEfforts {
            if case .object(let o) = $0, case .string(let id)? = o["activity_id"] { return id == activityId }
            return true
        }.filter { $0.activityId == activityId }
    }

    /// "Done as planned" for a slot, starting from the round before and the last time you did it here.
    public func asPlanned(_ slot: Slot, in workout: Workout) -> Metrics {
        let last = lastTime(slot.activityId, excludingSession: activeSession?.id, at: currentLocationId)
        return Prefill.asPlanned(slot, previous: workout.previous(slot), lastTime: last.elsewhere ? nil : last.efforts.last,
                                 atAnotherGym: plannedElsewhere(workout.session))
    }

    /// Your order and swaps for a planned session, kept on this phone; what you did goes in the logged sets.
    public func changes(for plannedSessionId: String) -> WorkoutChanges {
        state.workouts?[plannedSessionId] ?? WorkoutChanges()
    }

    public func workout(_ planned: PlannedSession) -> Workout {
        Workout(session: planned, changes: changes(for: planned.id), logged: logged(for: planned), library: library)
    }

    public mutating func setOrder(_ effortIds: [String], for plannedSessionId: String) {
        var updated = changes(for: plannedSessionId)
        updated.order = effortIds
        setChanges(updated, for: plannedSessionId)
    }

    /// Do another activity in place of a planned effort; nil goes back to the plan.
    public mutating func swap(_ effortId: String, to swap: Swap?, for plannedSessionId: String) {
        var updated = changes(for: plannedSessionId)
        var swaps = updated.swaps ?? [:]
        swaps[effortId] = swap
        updated.swaps = swaps.isEmpty ? nil : swaps
        setChanges(updated, for: plannedSessionId)
    }

    mutating func setChanges(_ changes: WorkoutChanges, for plannedSessionId: String) {
        let order = changes.order?.isEmpty == false ? changes.order : nil
        let swaps = changes.swaps?.isEmpty == false ? changes.swaps : nil
        var all = state.workouts ?? [:]
        all[plannedSessionId] = order == nil && swaps == nil ? nil : WorkoutChanges(order: order, swaps: swaps)
        state.workouts = all
    }

    /// The first planned session from `today` on that you haven't finished or logged in full.
    public func upNext(today: String) -> PlannedSession? {
        let finished = Set(finishedSessions.compactMap(\.plannedSessionId))
        return (state.plan?.days ?? []).filter { $0.date >= today }.flatMap(\.sessions)
            .first { !finished.contains($0.id) && workout($0).next != nil }
    }

    /// The equipment you have where you are: the location's, else your default; nil means everything counts.
    public var equipment: Set<String>? {
        (location(currentLocationId)?.equipment ?? defaultEquipment).map(Set.init)
    }

    /// Sets the equipment at where you are, or your default when you haven't picked a location.
    public mutating func setEquipment(_ items: Set<String>?) {
        if var here = location(currentLocationId) {
            here.payload.equipment = items?.sorted()
            put("location", here.id, here.payload)
            return
        }
        let value = items.map { JSONValue.array($0.sorted().map(JSONValue.string)) } ?? .null
        put("setting", SettingId.equipment.rawValue, SettingPayload(value: value))
    }

    /// Swaps for a planned effort, ranked against the activity the plan asked for.
    public func alternatives(for effort: PlannedEffort, limit: Int = 8) -> [Alternative] {
        guard let original = library.first(where: { $0.id == effort.activityId }) else { return [] }
        return Swaps.alternatives(to: original, in: library, equipment: equipment, limit: limit)
    }

    /// Finished sessions logged on this phone, newest first.
    public var finishedSessions: [PerformedSession] {
        sessions.filter { $0.endedAt != nil }
    }
}
