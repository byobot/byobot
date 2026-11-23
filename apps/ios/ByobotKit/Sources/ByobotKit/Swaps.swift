// Swaps ranked by the muscles they share and the equipment you have.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

public enum ActivityLibrary {
    /// Every piece of equipment the activities mention, sorted.
    public static func equipment(in library: [Activity]) -> [String] {
        Set(library.flatMap { $0.equipment ?? [] }).sorted()
    }
}

public struct Alternative: Equatable, Sendable, Identifiable {
    public var activity: Activity
    public var score: Double
    /// Equipment it needs that you said you don't have.
    public var missingEquipment: [String]
    public var id: String { activity.id }
}

public enum Swaps {
    static let kinds: Set<String> = ["strength", "cardio", "conditioning", "mobility", "core"]

    /// Activities that work the same muscles, ones you have the equipment for first.
    /// `equipment` nil means you have everything.
    public static func alternatives(to original: Activity, in library: [Activity], equipment: Set<String>? = nil,
                                    limit: Int = 8) -> [Alternative] {
        let p = Set(original.targets?.main ?? []), s = Set(original.targets?.synergists ?? [])
        guard !p.isEmpty else { return [] }
        let kinds = Set(original.tags ?? []).intersection(Self.kinds)
        var out: [Alternative] = []
        for candidate in library where candidate.id != original.id {
            let cp = Set(candidate.targets?.main ?? []), cs = Set(candidate.targets?.synergists ?? [])
            guard !p.isDisjoint(with: cp) else { continue }
            if !kinds.isEmpty, kinds.isDisjoint(with: candidate.tags ?? []) { continue }
            let shared: Double = 3 * Double(p.intersection(cp).count)
            let support: Double = Double(p.intersection(cs).count) + Double(s.intersection(cp).count)
                + 0.5 * Double(s.intersection(cs).count)
            let penalty: Double = 1.5 * Double(cp.subtracting(p).count) + Double(p.subtracting(cp).count)
            let score: Double = shared + support - penalty
            guard score > 0 else { continue }
            let missing = equipment.map { have in (candidate.equipment ?? []).filter { !have.contains($0) } } ?? []
            out.append(Alternative(activity: candidate, score: score, missingEquipment: missing))
        }
        out.sort { a, b in
            if a.missingEquipment.isEmpty != b.missingEquipment.isEmpty { return a.missingEquipment.isEmpty }
            if a.score != b.score { return a.score > b.score }
            return a.activity.name < b.activity.name
        }
        return Array(out.prefix(limit))
    }
}
