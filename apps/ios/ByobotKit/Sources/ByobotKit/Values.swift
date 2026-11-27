// Hand-written support for the generated contract types: JSON values, scalars,
// measures and the metric registry's entry type.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Any JSON value. Used where the contract allows anything (a source's raw record, error details).
public enum JSONValue: Codable, Equatable, Hashable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else if let a = try? c.decode([JSONValue].self) {
            self = .array(a)
        } else {
            self = .object(try c.decode([String: JSONValue].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try encodeNumber(n, into: &c)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    /// Decode any JSON text.
    public static func parse(_ data: Data) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: data)
    }
}

/// Whole numbers are written without a fraction, so `5` stays `5` (the contract wants integer reps).
func encodeNumber(_ n: Double, into c: inout SingleValueEncodingContainer) throws {
    if n.rounded() == n, abs(n) < 9_007_199_254_740_992 {
        try c.encode(Int64(n))
    } else {
        try c.encode(n)
    }
}

extension Sequence where Element == UInt8 {
    /// Lowercase hex, two digits a byte.
    var hex: String { map { String(format: "%02x", $0) }.joined() }

    /// Sixteen bytes written as a UUID.
    var uuid: String {
        let h = hex
        return [h.prefix(8), h.dropFirst(8).prefix(4), h.dropFirst(12).prefix(4), h.dropFirst(16).prefix(4),
                h.dropFirst(20)].joined(separator: "-")
    }
}

/// A number, text or true/false: a measure's value, or a sample's.
public enum Scalar: Codable, Equatable, Hashable, Sendable {
    case number(Double)
    case string(String)
    case bool(Bool)

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let n = try? c.decode(Double.self) {
            self = .number(n)
        } else {
            self = .string(try c.decode(String.self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .number(let n): try encodeNumber(n, into: &c)
        case .string(let s): try c.encode(s)
        case .bool(let b): try c.encode(b)
        }
    }

    public var number: Double? {
        if case .number(let n) = self { return n }
        return nil
    }

    /// How the value reads in the app: `5`, `82.5`, `V4`, `yes`.
    public var text: String {
        switch self {
        case .number(let n): return formatNumber(n)
        case .string(let s): return s
        case .bool(let b): return b ? "yes" : "no"
        }
    }
}

/// A number without a trailing `.0`, and at most three decimals.
public func formatNumber(_ n: Double) -> String {
    if n.rounded() == n, abs(n) < 1e15 { return String(Int64(n)) }
    var s = String(format: "%.3f", n)
    while s.hasSuffix("0") { s.removeLast() }
    if s.hasSuffix(".") { s.removeLast() }
    return s
}

/// One measurement: an exact `value`, or a range with `min`, `max` or both, plus a unit where the metric has one.
/// In a plan it is the target; in a log it is what happened.
public struct Measure: Codable, Equatable, Hashable, Sendable {
    public var value: Scalar?
    public var min: Double?
    public var max: Double?
    public var unit: String?

    public init(value: Scalar? = nil, min: Double? = nil, max: Double? = nil, unit: String? = nil) {
        self.value = value
        self.min = min
        self.max = max
        self.unit = unit
    }

    public static func exact(_ value: Double, unit: String? = nil) -> Measure {
        Measure(value: .number(value), unit: unit)
    }

    public static func text(_ value: String) -> Measure {
        Measure(value: .string(value))
    }

    enum CodingKeys: String, CodingKey {
        case value, min, max, unit
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(value, forKey: .value)
        try c.encodeIfPresent(min.map(Scalar.number), forKey: .min)
        try c.encodeIfPresent(max.map(Scalar.number), forKey: .max)
        try c.encodeIfPresent(unit, forKey: .unit)
    }

    /// The number a logging field starts from: the exact target, else the bottom of the range.
    public var startingNumber: Double? {
        value?.number ?? min ?? max
    }

    /// How the measure reads: `5`, `6–8`, `≥ 8`, `≤ 90 s`, `100 kg`.
    public var display: String {
        let amount: String
        if let value {
            amount = value.text
        } else if let min, let max {
            amount = min == max ? formatNumber(min) : "\(formatNumber(min))–\(formatNumber(max))"
        } else if let min {
            amount = "≥ \(formatNumber(min))"
        } else if let max {
            amount = "≤ \(formatNumber(max))"
        } else {
            amount = "?"
        }
        return unit.map { "\(amount) \(UnitLabel.text($0))" } ?? amount
    }
}

/// One code in the contract's registry.
public struct MetricSpec: Equatable, Hashable, Sendable {
    public enum Kind: String, Sendable {
        case integer, number, text
    }

    public var name: String
    public var summary: String
    /// Accepted UCUM units, the code's own first. Empty for unitless codes.
    public var units: [String]
    /// Each unit's factor to the code's own, which the bounds are in. A unit with none isn't checked against them.
    public var factors: [String: Double]
    public var unitRequired: Bool
    public var kind: Kind
    public var minimum: Double?
    /// The value must be above `minimum`, not at it.
    public var exclusiveMinimum: Bool
    public var maximum: Double?
    public var pattern: String?
    /// The values a category takes.
    public var values: [String]?
    /// The kinds it may be written on.
    public var on: [String]

    public init(name: String, summary: String, units: [String], factors: [String: Double] = [:], unitRequired: Bool,
                kind: Kind, minimum: Double?, exclusiveMinimum: Bool = false, maximum: Double?, pattern: String?,
                values: [String]? = nil, on: [String] = []) {
        self.name = name
        self.summary = summary
        self.units = units
        self.factors = factors
        self.unitRequired = unitRequired
        self.kind = kind
        self.minimum = minimum
        self.exclusiveMinimum = exclusiveMinimum
        self.maximum = maximum
        self.pattern = pattern
        self.values = values
        self.on = on
    }

    /// Whether `n` in `unit` is within the bounds, converted to the code's own unit as the laptop checks them.
    public func inBounds(_ n: Double, unit: String?) -> Bool {
        let factor: Double? = units.isEmpty ? 1 : unit.flatMap { factors[$0] }
        guard let factor else { return true }
        let x = n * factor
        if let lo = minimum, x < lo || (exclusiveMinimum && x == lo) { return false }
        if let hi = maximum, x > hi { return false }
        return true
    }

    /// A readable label: `heart_rate` → `Heart rate`, `x.grade` → `Grade`.
    public static func label(for name: String) -> String {
        var base = name.hasPrefix("x.") ? String(name.dropFirst(2)) : name
        base = base.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: ".", with: " ")
        switch base {
        case "rpe": return "RPE"
        case "rir": return "RIR"
        default: return base.prefix(1).uppercased() + base.dropFirst()
        }
    }
}

extension MetricRegistry {
    public static func spec(_ name: String) -> MetricSpec? {
        all.first { $0.name == name }
    }

    public static func isCustom(_ name: String) -> Bool {
        name.hasPrefix("x.") && name.range(of: codePattern, options: .regularExpression) != nil
    }

    /// Where a metric sorts when the app lists several: registry order, then custom metrics by name.
    static func order(_ name: String) -> Int {
        all.firstIndex { $0.name == name } ?? all.count
    }
}

/// UCUM units as the app shows them: `[lb_av]` → `lb`, `/min` → `bpm`.
public enum UnitLabel {
    static let labels = ["[lb_av]": "lb", "[mi_i]": "mi", "[yd_i]": "yd", "[ft_i]": "ft", "/min": "bpm",
                         "Cel": "°C", "mm[Hg]": "mmHg", "s/[mi_i]": "s/mi", "s/(100.m)": "s/100 m",
                         "s/(500.m)": "s/500 m", "mL/(kg.min)": "mL/kg/min"]

    public static func text(_ unit: String) -> String {
        labels[unit] ?? unit
    }
}
