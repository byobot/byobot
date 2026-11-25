// Apple Health readings as byobot telemetry: body composition, heart rate variability, resting heart rate,
// sleep and workouts. Scales, rings, watches and WHOOP write to Apple Health through their own apps; the app
// reads them there and sends them to the laptop, which decides which source wins for each metric.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// The app that wrote a reading to Apple Health, and the device that measured it when HealthKit knows.
public struct HealthOrigin: Equatable, Sendable {
    public var sourceName: String
    public var sourceBundleId: String
    public var deviceName: String?
    public var deviceManufacturer: String?
    public var deviceModel: String?
    /// HealthKit's local or UDI identifier for the device, when it has one.
    public var deviceIdentifier: String?

    public init(sourceName: String, sourceBundleId: String, deviceName: String? = nil,
                deviceManufacturer: String? = nil, deviceModel: String? = nil, deviceIdentifier: String? = nil) {
        self.sourceName = sourceName
        self.sourceBundleId = sourceBundleId
        self.deviceName = deviceName
        self.deviceManufacturer = deviceManufacturer
        self.deviceModel = deviceModel
        self.deviceIdentifier = deviceIdentifier
    }
}

/// A workout's totals, in the units the contract's workout_totals uses.
public struct WorkoutTotals: Equatable, Sendable {
    public var durationS: Double
    public var energyKcal: Double?
    public var distanceM: Double?
    public var heartRateAvgBpm: Double?
    public var heartRateMaxBpm: Double?

    public init(durationS: Double, energyKcal: Double? = nil, distanceM: Double? = nil,
                heartRateAvgBpm: Double? = nil, heartRateMaxBpm: Double? = nil) {
        self.durationS = durationS
        self.energyKcal = energyKcal
        self.distanceM = distanceM
        self.heartRateAvgBpm = heartRateAvgBpm
        self.heartRateMaxBpm = heartRateMaxBpm
    }
}

public enum HealthValue: Equatable, Sendable {
    /// A quantity already in the contract's unit for its type (see HealthMapping.units).
    case quantity(TelemetryType, Double)
    /// HealthKit's sleep analysis value, as its raw number.
    case sleep(Int)
    /// A HealthKit activity type name, such as `traditionalStrengthTraining`.
    case workout(String, WorkoutTotals)
}

/// One sample as HealthKit reports it, without depending on HealthKit (so it can be tested).
public struct HealthReading: Equatable, Sendable {
    public var uuid: UUID
    public var start: Date
    public var end: Date
    public var value: HealthValue
    public var origin: HealthOrigin

    public init(uuid: UUID, start: Date, end: Date, value: HealthValue, origin: HealthOrigin) {
        self.uuid = uuid
        self.start = start
        self.end = end
        self.value = value
        self.origin = origin
    }
}

public enum HealthMapping {
    /// The telemetry source for anything read from Apple Health. The device that measured it is kept on the sample.
    public static let source = "apple-health"

    /// The quantity types the app reads, with the contract's unit and the largest value it accepts.
    public static let units: [TelemetryType: (unit: String, max: Double)] = [
        .bodyMass: ("kg", 700), .leanBodyMass: ("kg", 700), .bodyFatPercentage: ("%", 100),
        .vo2Max: ("ml/kg*min", 100), .heartRateVariabilitySDNN: ("ms", 1000), .restingHeartRate: ("count/min", 300),
        .respiratoryRate: ("count/min", 100), .oxygenSaturation: ("%", 100),
    ]

    /// HealthKit's sleep analysis values by raw number, as the contract names them.
    public static let sleepStages = [0: "inBed", 1: "asleepUnspecified", 2: "awake", 3: "asleepCore",
                                     4: "asleepDeep", 5: "asleepREM"]

    /// A telemetry sample keyed on HealthKit's sample UUID, so sending it again changes nothing.
    /// Readings the contract would refuse (zero, negative, absurd) are skipped.
    public static func sample(_ r: HealthReading) -> TelemetrySample? {
        guard r.end >= r.start else { return nil }
        let value: Scalar
        var unit: String?
        var raw: JSONValue?
        switch r.value {
        case .quantity(let type, let amount):
            guard let spec = units[type], amount.isFinite, amount <= spec.max,
                  amount > 0 || (amount == 0 && spec.unit == "%") else { return nil }
            value = .number((amount * 1000).rounded() / 1000)
            unit = spec.unit
        case .sleep(let stage):
            guard let name = sleepStages[stage] else { return nil }
            value = .string(name)
        case .workout(let activity, let totals):
            guard activity.range(of: "^[a-z][A-Za-z0-9]{0,63}$", options: .regularExpression) != nil,
                  totals.durationS.isFinite, totals.durationS >= 0 else { return nil }
            value = .string(activity)
            raw = .object(workoutTotals(totals))
        }
        return TelemetrySample(source: source, sourceId: r.uuid.uuidString.lowercased(), type: telemetryType(r.value),
                               start: Timestamp.string(r.start), end: Timestamp.string(r.end), value: value,
                               unit: unit, device: device(r.origin), raw: raw)
    }

    static func telemetryType(_ value: HealthValue) -> TelemetryType {
        switch value {
        case .quantity(let type, _): return type
        case .sleep: return .sleepAnalysis
        case .workout: return .workout
        }
    }

    static func workoutTotals(_ t: WorkoutTotals) -> [String: JSONValue] {
        var out: [String: JSONValue] = ["duration_s": .number((t.durationS * 1000).rounded() / 1000)]
        let optional: [(String, Double?, Double)] = [
            ("energy_kcal", t.energyKcal, .infinity), ("distance_m", t.distanceM, .infinity),
            ("heart_rate_avg_bpm", t.heartRateAvgBpm, 300), ("heart_rate_max_bpm", t.heartRateMaxBpm, 300),
        ]
        for (key, value, max) in optional {
            if let value, value.isFinite, value >= 0, value <= max, !(key.hasPrefix("heart") && value == 0) {
                out[key] = .number((value * 10).rounded() / 10)
            }
        }
        return out
    }

    static func device(_ o: HealthOrigin) -> TelemetryDevice? {
        let id = [o.deviceIdentifier, o.sourceBundleId].compactMap { $0 }.first { !$0.isEmpty }
        return id.map {
            TelemetryDevice(id: String($0.prefix(200)), name: clip(o.deviceName ?? o.sourceName),
                            manufacturer: clip(o.deviceManufacturer), model: clip(o.deviceModel))
        }
    }

    static func clip(_ text: String?) -> String? {
        guard let text, !text.isEmpty else { return nil }
        return String(text.prefix(200))
    }
}
