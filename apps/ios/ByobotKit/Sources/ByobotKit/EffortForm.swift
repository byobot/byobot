// Turning a planned effort into fields to fill in, and the filled fields back into metrics.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// One metric to log, with the plan's target for it.
public struct MetricField: Identifiable, Equatable, Sendable {
    public var name: String
    public var target: Measure?
    /// What the person typed.
    public var text: String
    public var unit: String?

    public var id: String { name }
    public var spec: MetricSpec? { MetricRegistry.spec(name) }
    public var label: String { MetricSpec.label(for: name) }

    /// Units to offer: the registry's for this metric, or whatever the plan used for a custom one.
    public var units: [String] {
        if let spec { return spec.units }
        return unit.map { [$0] } ?? []
    }

    public init(name: String, target: Measure? = nil, text: String = "", unit: String? = nil) {
        self.name = name
        self.target = target
        self.text = text
        self.unit = unit
    }
}

public enum EffortFormError: Error, Equatable, LocalizedError {
    case notANumber(String)
    case notAWholeNumber(String)
    case outOfRange(String)
    case badFormat(String)

    public var errorDescription: String? {
        switch self {
        case .notANumber(let m): return "\(MetricSpec.label(for: m)) needs a number."
        case .notAWholeNumber(let m): return "\(MetricSpec.label(for: m)) needs a whole number."
        case .outOfRange(let m): return "\(MetricSpec.label(for: m)) is out of range."
        case .badFormat(let m): return "\(MetricSpec.label(for: m)) isn't in the expected format."
        }
    }
}

public enum EffortForm {
    /// Fields for logging an effort: the planned effort's metrics, then the activity's usual ones.
    /// Each starts from the exact target where the plan has one; a range target shows as a hint instead.
    public static func fields(planned: PlannedEffort?, activity: Activity?) -> [MetricField] {
        var names: [String] = []
        for name in (planned?.metrics ?? [:]).keys.sorted(by: metricOrder) where !names.contains(name) {
            names.append(name)
        }
        for name in activity?.defaultMetrics ?? [] where !names.contains(name) {
            names.append(name)
        }
        return names.map { field(name: $0, target: planned?.metrics?[$0]) }
    }

    /// Fields for correcting a logged set: the usual ones, holding what was logged, plus any other metric it has.
    public static func fields(planned: PlannedEffort?, activity: Activity?, filledFrom metrics: Metrics?) -> [MetricField] {
        var fields = self.fields(planned: planned, activity: activity)
        for i in fields.indices { fields[i].text = "" }
        for (name, measure) in (metrics ?? [:]).sorted(by: { metricOrder($0.key, $1.key) }) {
            if !fields.contains(where: { $0.name == name }) { fields.append(field(name: name, target: nil)) }
            let i = fields.firstIndex { $0.name == name }!
            fields[i].text = measure.value?.text ?? measure.startingNumber.map(formatNumber) ?? ""
            fields[i].unit = measure.unit ?? fields[i].unit
        }
        return fields
    }

    public static func field(name: String, target: Measure?) -> MetricField {
        var text = ""
        if let value = target?.value {
            text = value.text
        }
        let unit = target?.unit ?? MetricRegistry.spec(name)?.units.first
        return MetricField(name: name, target: target, text: text, unit: unit)
    }

    static func metricOrder(_ a: String, _ b: String) -> Bool {
        let (oa, ob) = (MetricRegistry.order(a), MetricRegistry.order(b))
        return oa != ob ? oa < ob : a < b
    }

    /// Metrics from the filled fields. Empty fields are left out.
    public static func metrics(from fields: [MetricField]) throws -> Metrics {
        var out: Metrics = [:]
        for field in fields {
            if let measure = try measure(field) {
                out[field.name] = measure
            }
        }
        return out
    }

    static func measure(_ field: MetricField) throws -> Measure? {
        let text = field.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        guard let spec = field.spec else {
            // A custom metric: a number if it reads as one, otherwise text (a climbing grade, a band colour).
            if let n = parseNumber(text) { return Measure(value: .number(n), unit: field.unit) }
            return Measure(value: .string(text))
        }
        let unit = spec.units.isEmpty ? nil : (field.unit.flatMap { spec.units.contains($0) ? $0 : nil } ?? spec.units.first)
        switch spec.kind {
        case .text:
            if let pattern = spec.pattern, text.range(of: pattern, options: .regularExpression) == nil {
                throw EffortFormError.badFormat(field.name)
            }
            return Measure(value: .string(text), unit: unit)
        case .integer, .number:
            guard let n = parseNumber(text) else { throw EffortFormError.notANumber(field.name) }
            if spec.kind == .integer, n.rounded() != n { throw EffortFormError.notAWholeNumber(field.name) }
            if !spec.inBounds(n, unit: unit) { throw EffortFormError.outOfRange(field.name) }
            return Measure(value: .number(n), unit: unit)
        }
    }

    /// Accepts `82.5`, `82,5`, `1,000` and `1.000,5`. With a comma and a point, the last is the decimal mark; a
    /// lone comma is one unless exactly three digits follow it. The other mark groups thousands, in threes.
    static func parseNumber(_ text: String) -> Double? {
        var mark: Character = "."
        if let comma = text.lastIndex(of: ",") {
            if let point = text.lastIndex(of: ".") {
                if comma > point { mark = "," }
            } else {
                let after = text[text.index(after: comma)...]
                let thousands = after.count == 3 && after.allSatisfy { $0.isASCII && $0.isNumber }
                if text.filter({ $0 == "," }).count == 1 && !thousands { mark = "," }
            }
        }
        let group: Character = mark == "," ? "." : ","
        var whole = Substring(text), fraction: Substring?
        if let i = text.lastIndex(of: mark) {
            whole = text[..<i]
            fraction = text[text.index(after: i)...]
        }
        if fraction?.contains(group) == true || whole.contains(mark) { return nil }
        if whole.contains(group) {
            let groups = whole.split(separator: group, omittingEmptySubsequences: false)
            let lead = groups[0].drop { $0 == "-" || $0 == "+" }
            guard (1...3).contains(lead.count),
                  groups.dropFirst().allSatisfy({ $0.count == 3 && $0.allSatisfy { $0.isASCII && $0.isNumber } }) else {
                return nil
            }
            whole = Substring(groups.joined())
        }
        guard let n = Double(fraction.map { "\(whole).\($0)" } ?? String(whole)), n.isFinite else { return nil }
        return n
    }
}
