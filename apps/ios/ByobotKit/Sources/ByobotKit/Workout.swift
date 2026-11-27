// A planned session as you work through it: in your order, with your swaps, one slot per effort per round.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Your order and swaps for a planned session, kept on this phone.
public struct WorkoutChanges: Codable, Equatable, Sendable {
    /// Planned effort ids in the order you want them.
    public var order: [String]?
    /// Planned effort id to the activity you're doing instead.
    public var swaps: [String: Swap]?

    public init(order: [String]? = nil, swaps: [String: Swap]? = nil) {
        self.order = order
        self.swaps = swaps
    }
}

/// Another activity done in place of a planned effort, and why.
public struct Swap: Codable, Equatable, Hashable, Sendable {
    public var activityId: String
    public var reason: String?

    public init(activityId: String, reason: String? = nil) {
        self.activityId = activityId
        self.reason = reason
    }

    enum CodingKeys: String, CodingKey {
        case activityId = "activity_id"
        case reason
    }
}

/// One planned effort in one round.
public struct Slot: Identifiable, Equatable, Sendable {
    public var block: Block
    public var effort: PlannedEffort
    public var round: Int
    public var rounds: Int
    /// The activity to do: the planned one, or its swap.
    public var activityId: String
    public var swap: Swap?
    public var logged: PerformedEffort?
    /// The last effort of its block in this round, so the block's rest follows it.
    public var lastInRound: Bool
    /// A timed hold done on each side in turn.
    public var perSide: Bool

    public var id: String { "\(effort.id):\(round)" }
    public var isTimed: Bool { effort.metrics?["duration"] != nil && effort.metrics?["reps"] == nil }
    /// How long its work lasts on a clock: its own duration, else the block's per round (an EMOM).
    public var workSeconds: TimeInterval? {
        (isTimed ? effort.metrics?["duration"]?.seconds : nil) ?? block.metrics?["duration"]?.seconds
    }

    /// Rest after this slot: the effort's own, else the block's at the end of a round.
    public var restSeconds: TimeInterval? {
        if let rest = effort.metrics?["rest"]?.seconds { return rest }
        return lastInRound ? block.metrics?["rest"]?.seconds : nil
    }
}

public struct Exercise: Identifiable, Equatable, Sendable {
    public var block: Block
    public var effort: PlannedEffort
    public var activityId: String
    public var swap: Swap?
    public var done: Int
    public var total: Int
    public var id: String { effort.id }
}

public struct Workout: Sendable {
    public let session: PlannedSession
    public let changes: WorkoutChanges
    public let logged: [String: [Int: PerformedEffort]]
    public let library: [Activity]

    public init(session: PlannedSession, changes: WorkoutChanges = WorkoutChanges(),
                logged: [String: [Int: PerformedEffort]] = [:], library: [Activity] = []) {
        self.session = session
        self.changes = changes
        self.logged = logged
        self.library = library
    }

    public func activity(_ id: String) -> Activity? { library.first { $0.id == id } }
    public func name(_ id: String) -> String { activity(id)?.name ?? id }

    /// Planned efforts with their blocks, in your order; any you haven't placed keep the plan's order after.
    var ordered: [(block: Block, effort: PlannedEffort)] {
        let all = session.blocks.flatMap { b in b.efforts.map { (block: b, effort: $0) } }
        let rank = Dictionary((changes.order ?? []).enumerated().map { ($1, $0) }, uniquingKeysWith: { a, _ in a })
        return all.enumerated()
            .sorted { (rank[$0.element.effort.id] ?? Int.max, $0.offset) < (rank[$1.element.effort.id] ?? Int.max, $1.offset) }
            .map(\.element)
    }

    public var exercises: [Exercise] {
        ordered.map { item in
            let rounds = max(1, item.block.rounds ?? 1)
            let swap = changes.swaps?[item.effort.id]
            return Exercise(block: item.block, effort: item.effort, activityId: swap?.activityId ?? item.effort.activityId,
                            swap: swap, done: logged[item.effort.id]?.count ?? 0, total: rounds)
        }
    }

    /// Every slot in the order you'll do them. A block stays together (a superset alternates within each
    /// round); blocks come in the order of their first exercise.
    public var slots: [Slot] {
        let items = ordered
        var blockIds: [String] = []
        for item in items where !blockIds.contains(item.block.id) { blockIds.append(item.block.id) }
        var out: [Slot] = []
        for id in blockIds {
            let group = items.filter { $0.block.id == id }
            guard let block = group.first?.block else { continue }
            let rounds = max(1, block.rounds ?? 1)
            for round in 1...rounds {
                for (i, item) in group.enumerated() {
                    let swap = changes.swaps?[item.effort.id]
                    let activityId = swap?.activityId ?? item.effort.activityId
                    let unilateral = activity(activityId)?.tags?.contains("unilateral") ?? false
                    var slot = Slot(block: block, effort: item.effort, round: round, rounds: rounds, activityId: activityId,
                                    swap: swap, logged: logged[item.effort.id]?[round], lastInRound: i == group.count - 1,
                                    perSide: false)
                    slot.perSide = slot.isTimed && unilateral && (item.effort.side ?? .both) == .both
                    out.append(slot)
                }
            }
        }
        return out
    }

    /// What you logged for the same effort in the round before.
    public func previous(_ slot: Slot) -> PerformedEffort? { logged[slot.effort.id]?[slot.round - 1] }

    public var next: Slot? { slots.first { $0.logged == nil } }
    public var done: Int { slots.filter { $0.logged != nil }.count }
    public var total: Int { slots.count }

    /// A block to run on an interval clock: several rounds, each timed by the block or by every effort.
    public func isIntervalBlock(_ block: Block) -> Bool {
        (block.rounds ?? 1) > 1 && (block.metrics?["duration"] != nil
            || block.efforts.allSatisfy { e in e.metrics?["duration"] != nil && e.metrics?["reps"] == nil })
    }

    /// The slots to run on a clock from `slot`: the rest of its interval block, or a timed hold on its own.
    public func clockSlots(from slot: Slot) -> [Slot]? {
        if isIntervalBlock(slot.block) {
            return slots.filter { $0.block.id == slot.block.id && $0.logged == nil }
        }
        return slot.isTimed ? [slot] : nil
    }

    /// Notes for a set: only what you typed, the reason you gave for a swap and then your note.
    public func notes(for slot: Slot, _ extra: String?) -> String? {
        let parts = [slot.swap?.reason, extra].compactMap { $0?.isEmpty == false ? $0 : nil }
        return parts.isEmpty ? nil : parts.joined(separator: ". ")
    }
}

extension Measure {
    /// A duration or rest in seconds: the exact value, else the bottom of the range.
    public var seconds: TimeInterval? {
        guard let n = startingNumber else { return nil }
        switch unit {
        case "min": return n * 60
        case "h": return n * 3600
        default: return n
        }
    }
}
