// Apple Health readings as samples for the laptop: body measurements, heart, activity, mobility, vitals, sleep
// and workouts. Scales, rings, watches and WHOOP write to Apple Health through their own apps; the app
// reads them there and sends them to the laptop, which decides which source wins for each metric.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// The app that wrote a reading to Apple Health, and the device that measured it when HealthKit knows.
public struct HealthOrigin: Equatable, Sendable {
    public var sourceName: String
    public var sourceBundleId: String
    public var deviceName: String?

    public init(sourceName: String, sourceBundleId: String, deviceName: String? = nil) {
        self.sourceName = sourceName
        self.sourceBundleId = sourceBundleId
        self.deviceName = deviceName
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
    /// A quantity of a HealthKit type, already in its registry code's own unit.
    case quantity(String, Double)
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
    /// Where it was taken, when HealthKit says (Apple Watch workouts and sleep do). Else the phone's zone.
    public var zone: TimeZone?

    public init(uuid: UUID, start: Date, end: Date, value: HealthValue, origin: HealthOrigin, zone: TimeZone? = nil) {
        self.uuid = uuid
        self.start = start
        self.end = end
        self.value = value
        self.origin = origin
        self.zone = zone
    }
}

/// A day's total, or for heart rate its average, as HealthKit's statistics give it across every app and device.
public struct DailyTotal: Equatable, Sendable {
    /// The HealthKit type, such as `HKQuantityTypeIdentifierStepCount`.
    public var type: String
    public var start: Date
    public var end: Date
    public var value: Double
    public var lowest: Double?
    public var highest: Double?

    public init(type: String, start: Date, end: Date, value: Double, lowest: Double? = nil,
                highest: Double? = nil) {
        self.type = type
        self.start = start
        self.end = end
        self.value = value
        self.lowest = lowest
        self.highest = highest
    }
}

public enum HealthMapping {
    /// Where anything read from Apple Health comes from. The device that measured it is kept on the sample.
    public static let source = "apple_health"
    public static let workoutType = "HKWorkoutTypeIdentifier"
    public static let sleepType = "HKCategoryTypeIdentifierSleepAnalysis"

    /// HealthKit's sleep analysis values by raw number, as the registry names them.
    public static let sleepStages = [0: "in_bed", 1: "asleep_unspecified", 2: "awake", 3: "asleep_core",
                                     4: "asleep_deep", 5: "asleep_rem"]

    /// The registry code a HealthKit type is read as.
    private static func spec(_ type: String) -> MetricSpec? {
        MetricRegistry.healthkit[type].flatMap(MetricRegistry.spec)
    }

    /// A sample keyed on HealthKit's sample UUID, so sending it again changes nothing.
    /// Readings the contract would refuse (zero, negative, absurd) are skipped.
    public static func sample(_ r: HealthReading) -> Sample? {
        guard r.end >= r.start else { return nil }
        let type: String
        let value: Scalar
        var unit: String?
        var raw: JSONValue?
        switch r.value {
        case .quantity(let t, let amount):
            guard let spec = spec(t), amount.isFinite, amount <= (spec.maximum ?? .infinity),
                  amount > 0 || (amount == 0 && spec.units.first == "%") else { return nil }
            type = t
            value = .number(milli(amount))
            unit = spec.units.first
        case .sleep(let stage):
            guard let name = sleepStages[stage] else { return nil }
            type = sleepType
            value = .string(name)
        case .workout(let activity, let totals):
            guard activity.range(of: "^[a-z][A-Za-z0-9]{0,63}$", options: .regularExpression) != nil,
                  totals.durationS.isFinite, totals.durationS >= 0 else { return nil }
            type = workoutType
            value = .string(activity)
            raw = .object(workoutTotals(totals))
        }
        let zone = r.zone ?? .current
        return Sample(source: source, sourceId: r.uuid.uuidString.lowercased(), type: type,
                      start: Timestamp.string(r.start, zone: zone), end: Timestamp.string(r.end, zone: zone),
                      value: value, unit: unit,
                      device: device(r.origin), originApp: r.origin.sourceBundleId.isEmpty ? nil : r.origin.sourceBundleId,
                      raw: raw)
    }

    /// A day's total keyed on its type and local date. It carries when it was read, so a later read of a day
    /// still under way is a new revision of the same record.
    public static func sample(_ d: DailyTotal, readAt: Date, calendar: Calendar = .contract) -> Sample? {
        guard let spec = spec(d.type), d.value.isFinite, d.value > 0, d.value <= (spec.maximum ?? .infinity),
              d.end > d.start else { return nil }
        let day = calendar.dateComponents([.year, .month, .day], from: d.start)
        let key = String(format: "%04d-%02d-%02d", day.year ?? 0, day.month ?? 0, day.day ?? 0)
        var raw: [String: JSONValue] = ["modified_at": .string(Timestamp.string(readAt))]
        for (name, value) in [("min_bpm", d.lowest), ("max_bpm", d.highest)] {
            if let value, value.isFinite, value > 0, value <= (spec.maximum ?? .infinity) {
                raw[name] = .number(value.rounded())
            }
        }
        return Sample(source: source, sourceId: "daily:\(d.type):\(key)", type: d.type,
                      start: Timestamp.string(d.start, zone: calendar.timeZone),
                      end: Timestamp.string(d.end, zone: calendar.timeZone), value: .number(milli(d.value)),
                      unit: spec.units.first, raw: .object(raw))
    }

    /// What to send from a read of each day's totals since `since`. `sent` holds each day sent before, by source
    /// id, with the offset its day started at; the map that comes back is the one to keep once these are delivered.
    /// A day sent in another offset (you were away) is left as it was, since this read puts other hours on it. A
    /// day sent in this offset that has no total now is deleted.
    public static func days(_ totals: [DailyTotal], since: Date, sent: [String: Int], readAt: Date,
                            calendar: Calendar = .contract) -> (samples: [Sample], deleted: [Deletion], sent: [String: Int]) {
        let first = LocalDate.string(since, calendar: calendar)
        var kept = sent.filter { (dayOf($0.key) ?? "") >= first }
        var samples: [Sample] = [], read = Set<String>()
        for total in totals {
            guard let day = Self.sample(total, readAt: readAt, calendar: calendar) else { continue }
            read.insert(day.sourceId)
            let offset = calendar.timeZone.secondsFromGMT(for: total.start)
            if let was = kept[day.sourceId], was != offset { continue }
            kept[day.sourceId] = offset
            samples.append(day)
        }
        var deleted: [Deletion] = []
        for (key, offset) in kept.sorted(by: { $0.key < $1.key }) where !read.contains(key) {
            guard let day = dayOf(key), let noon = LocalDate.date(day, calendar: calendar),
                  calendar.timeZone.secondsFromGMT(for: calendar.startOfDay(for: noon)) == offset else { continue }
            let type = String(key.dropFirst("daily:".count).dropLast(day.count + 1))
            deleted.append(Deletion(source: source, sourceId: key, type: type))
            kept[key] = nil
        }
        return (samples, deleted, kept)
    }

    /// The date in a daily total's source id.
    static func dayOf(_ key: String) -> String? {
        guard key.hasPrefix("daily:"), let colon = key.lastIndex(of: ":") else { return nil }
        let day = String(key[key.index(after: colon)...])
        return day.count == 10 ? day : nil
    }

    /// A sample HealthKit says was deleted.
    public static func deletion(_ uuid: UUID, type: String) -> Deletion {
        Deletion(source: source, sourceId: uuid.uuidString.lowercased(), type: type)
    }

    static func workoutTotals(_ t: WorkoutTotals) -> [String: JSONValue] {
        var out: [String: JSONValue] = ["duration_s": .number(milli(t.durationS))]
        let heartRate = MetricRegistry.spec("heart_rate")?.maximum ?? .infinity
        let optional: [(String, Double?, Double)] = [
            ("energy_kcal", t.energyKcal, .infinity), ("distance_m", t.distanceM, .infinity),
            ("heart_rate_avg_bpm", t.heartRateAvgBpm, heartRate), ("heart_rate_max_bpm", t.heartRateMaxBpm, heartRate),
        ]
        for (key, value, max) in optional {
            if let value, value.isFinite, value >= 0, value <= max, !(key.hasPrefix("heart") && value == 0) {
                out[key] = .number((value * 10).rounded() / 10)
            }
        }
        return out
    }

    /// Rounded to three decimals.
    private static func milli(_ x: Double) -> Double {
        (x * 1000).rounded() / 1000
    }

    /// The device that measured it, else the app that wrote it.
    static func device(_ o: HealthOrigin) -> String? {
        guard let name = [o.deviceName, o.sourceName].compactMap({ $0 }).first(where: { !$0.isEmpty }) else { return nil }
        return String(name.prefix(200))
    }
}
