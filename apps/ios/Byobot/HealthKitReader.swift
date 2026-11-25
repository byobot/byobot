// Reading Apple Health: body measurements from your scale's app; heart, activity, mobility, sleep and workouts
// from your phone, watch, ring or band; and vitals such as blood pressure. byobot only reads, and never writes
// to Apple Health. Nutrition and cycle tracking are left out.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import Foundation
import HealthKit

final class HealthKitReader: @unchecked Sendable {
    private let store = HKHealthStore()

    /// Quantity types with the HealthKit unit to read them in, and the factor to their registry code's unit.
    private let quantities: [(HKQuantityTypeIdentifier, HKUnit, Double)] = [
        (.bodyMass, .gramUnit(with: .kilo), 1),
        (.leanBodyMass, .gramUnit(with: .kilo), 1),
        (.bodyFatPercentage, .percent(), 100),
        (.bodyMassIndex, .count(), 1),
        (.height, .meter(), 1),
        (.waistCircumference, .meter(), 1),
        (.vo2Max, HKUnit(from: "ml/kg*min"), 1),
        (.heartRateVariabilitySDNN, .secondUnit(with: .milli), 1),
        (.restingHeartRate, HealthKitReader.bpm, 1),
        (.walkingHeartRateAverage, HealthKitReader.bpm, 1),
        (.heartRateRecoveryOneMinute, HealthKitReader.bpm, 1),
        (.respiratoryRate, HealthKitReader.bpm, 1),
        (.oxygenSaturation, .percent(), 100),
        (.walkingSpeed, HealthKitReader.metersPerSecond, 1),
        (.walkingStepLength, .meter(), 1),
        (.walkingDoubleSupportPercentage, .percent(), 100),
        (.walkingAsymmetryPercentage, .percent(), 100),
        (.appleWalkingSteadiness, .percent(), 100),
        (.sixMinuteWalkTestDistance, .meter(), 1),
        (.stairAscentSpeed, HealthKitReader.metersPerSecond, 1),
        (.stairDescentSpeed, HealthKitReader.metersPerSecond, 1),
        (.bloodPressureSystolic, .millimeterOfMercury(), 1),
        (.bloodPressureDiastolic, .millimeterOfMercury(), 1),
        (.bloodGlucose, HKUnit(from: "mg/dL"), 1),
        (.bodyTemperature, .degreeCelsius(), 1),
        (.appleSleepingWristTemperature, .degreeCelsius(), 1),
    ]
    /// Read as one total per day across every app and device, since the phone and watch both count steps.
    private let totals: [(HKQuantityTypeIdentifier, HKUnit)] = [
        (.stepCount, .count()),
        (.flightsClimbed, .count()),
        (.distanceWalkingRunning, .meter()),
        (.distanceCycling, .meter()),
        (.distanceSwimming, .meter()),
        (.activeEnergyBurned, .kilocalorie()),
        (.basalEnergyBurned, .kilocalorie()),
        (.appleExerciseTime, .second()),
        (.appleStandTime, .second()),
    ]
    private static var bpm: HKUnit { .count().unitDivided(by: .minute()) }
    private static var metersPerSecond: HKUnit { .meter().unitDivided(by: .second()) }
    private let sleep = HKCategoryType(.sleepAnalysis)
    private let limit = 50_000

    var available: Bool { HKHealthStore.isHealthDataAvailable() }

    private var readTypes: Set<HKObjectType> {
        var types: Set<HKObjectType> = [sleep, HKObjectType.workoutType(), HKQuantityType(.heartRate),
                                        HKQuantityType(.activeEnergyBurned)]
        for (id, _, _) in quantities { types.insert(HKQuantityType(id)) }
        for (id, _) in totals { types.insert(HKQuantityType(id)) }
        for id in Self.distances { types.insert(HKQuantityType(id)) }
        return types
    }

    /// Ask to read. iOS doesn't tell apps whether reading was allowed, so a refusal just reads as no data.
    func requestAccess() async throws {
        try await store.requestAuthorization(toShare: [], read: readTypes)
    }

    /// Whether iOS would ask about types it hasn't asked about yet, such as ones a newer byobot reads.
    func needsAccess() async -> Bool {
        (try? await store.statusForAuthorizationRequest(toShare: [], read: readTypes)) == .shouldRequest
    }

    /// What changed in Apple Health since the saved anchors: samples added or edited, samples deleted, and the
    /// anchors to save once all of it has reached the laptop. Without an anchor a type is read back to `since`.
    func changes(since: Date, anchors: [String: Data]) async throws -> HealthChanges {
        let window = HKQuery.predicateForSamples(withStart: since, end: nil)
        var out = HealthChanges(readings: [], deleted: [], anchors: anchors)
        func anchor(_ key: String) -> HKQueryAnchor? {
            anchors[key].flatMap { try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0) }
        }
        func keep(_ key: String, _ new: HKQueryAnchor, _ deleted: [HKDeletedObject]) {
            out.deleted += deleted.map { ($0.uuid, key) }
            out.anchors[key] = try? NSKeyedArchiver.archivedData(withRootObject: new, requiringSecureCoding: true)
        }
        for (id, unit, factor) in quantities {
            let key = id.rawValue
            let query = HKAnchoredObjectQueryDescriptor(predicates: [.quantitySample(type: HKQuantityType(id), predicate: window)],
                                                        anchor: anchor(key), limit: limit)
            let result = try await query.result(for: store)
            out.readings += result.addedSamples.map { reading($0, .quantity(key, $0.quantity.doubleValue(for: unit) * factor)) }
            keep(key, result.newAnchor, result.deletedObjects)
        }
        let sleeps = HKAnchoredObjectQueryDescriptor(predicates: [.categorySample(type: sleep, predicate: window)],
                                                     anchor: anchor(HealthMapping.sleepType), limit: limit)
        let nights = try await sleeps.result(for: store)
        out.readings += nights.addedSamples.map { reading($0, .sleep($0.value)) }
        keep(HealthMapping.sleepType, nights.newAnchor, nights.deletedObjects)
        let workouts = HKAnchoredObjectQueryDescriptor(predicates: [.workout(window)],
                                                       anchor: anchor(HealthMapping.workoutType), limit: limit)
        let sessions = try await workouts.result(for: store)
        out.readings += sessions.addedSamples.map { reading($0, .workout(Self.name($0.workoutActivityType), workoutTotals($0))) }
        keep(HealthMapping.workoutType, sessions.newAnchor, sessions.deletedObjects)
        return out
    }

    /// Each day's totals, and its average, lowest and highest heart rate, from the start of the day `since` falls in.
    func dailyTotals(since: Date) async throws -> [DailyTotal] {
        let start = Calendar.current.startOfDay(for: since)
        let window = HKQuery.predicateForSamples(withStart: start, end: nil)
        let day = DateComponents(day: 1)
        var out: [DailyTotal] = []
        for (id, unit) in totals {
            let query = HKStatisticsCollectionQueryDescriptor(
                predicate: .quantitySample(type: HKQuantityType(id), predicate: window),
                options: .cumulativeSum, anchorDate: start, intervalComponents: day)
            let days = try await query.result(for: store)
            for stats in days.statistics() {
                if let sum = stats.sumQuantity() {
                    out.append(DailyTotal(type: id.rawValue, start: stats.startDate, end: stats.endDate,
                                          value: sum.doubleValue(for: unit)))
                }
            }
        }
        let heart = HKStatisticsCollectionQueryDescriptor(
            predicate: .quantitySample(type: HKQuantityType(.heartRate), predicate: window),
            options: [.discreteAverage, .discreteMin, .discreteMax], anchorDate: start, intervalComponents: day)
        let days = try await heart.result(for: store)
        for stats in days.statistics() {
            if let average = stats.averageQuantity() {
                out.append(DailyTotal(type: HKQuantityTypeIdentifier.heartRate.rawValue, start: stats.startDate, end: stats.endDate,
                                      value: average.doubleValue(for: Self.bpm),
                                      lowest: stats.minimumQuantity()?.doubleValue(for: Self.bpm),
                                      highest: stats.maximumQuantity()?.doubleValue(for: Self.bpm)))
            }
        }
        return out
    }

    private func reading(_ sample: HKSample, _ value: HealthValue) -> HealthReading {
        HealthReading(uuid: sample.uuid, start: sample.startDate, end: sample.endDate, value: value,
                      origin: HealthOrigin(sourceName: sample.sourceRevision.source.name,
                                           sourceBundleId: sample.sourceRevision.source.bundleIdentifier,
                                           deviceName: sample.device?.name,
                                           deviceManufacturer: sample.device?.manufacturer,
                                           deviceModel: sample.device?.model,
                                           deviceIdentifier: sample.device?.localIdentifier
                                               ?? sample.device?.udiDeviceIdentifier))
    }

    private static let distances: [HKQuantityTypeIdentifier] = [
        .distanceWalkingRunning, .distanceCycling, .distanceSwimming, .distanceDownhillSnowSports,
    ]

    private func workoutTotals(_ workout: HKWorkout) -> WorkoutTotals {
        let bpm = Self.bpm
        let heart = workout.statistics(for: HKQuantityType(.heartRate))
        let distance = Self.distances.lazy
            .compactMap { workout.statistics(for: HKQuantityType($0))?.sumQuantity()?.doubleValue(for: .meter()) }
            .first
        return WorkoutTotals(
            durationS: workout.duration,
            energyKcal: workout.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?
                .doubleValue(for: .kilocalorie()),
            distanceM: distance,
            heartRateAvgBpm: heart?.averageQuantity()?.doubleValue(for: bpm),
            heartRateMaxBpm: heart?.maximumQuantity()?.doubleValue(for: bpm))
    }

    /// HealthKit's name for an activity type. Types not listed here read as `other`.
    static func name(_ type: HKWorkoutActivityType) -> String {
        switch type {
        case .traditionalStrengthTraining: "traditionalStrengthTraining"
        case .functionalStrengthTraining: "functionalStrengthTraining"
        case .coreTraining: "coreTraining"
        case .highIntensityIntervalTraining: "highIntensityIntervalTraining"
        case .crossTraining: "crossTraining"
        case .mixedCardio: "mixedCardio"
        case .running: "running"
        case .walking: "walking"
        case .hiking: "hiking"
        case .cycling: "cycling"
        case .swimming: "swimming"
        case .rowing: "rowing"
        case .elliptical: "elliptical"
        case .stairClimbing: "stairClimbing"
        case .stairs: "stairs"
        case .jumpRope: "jumpRope"
        case .yoga: "yoga"
        case .pilates: "pilates"
        case .barre: "barre"
        case .flexibility: "flexibility"
        case .mindAndBody: "mindAndBody"
        case .preparationAndRecovery: "preparationAndRecovery"
        case .cooldown: "cooldown"
        case .boxing: "boxing"
        case .kickboxing: "kickboxing"
        case .martialArts: "martialArts"
        case .climbing: "climbing"
        case .crossCountrySkiing: "crossCountrySkiing"
        case .downhillSkiing: "downhillSkiing"
        case .snowboarding: "snowboarding"
        case .tennis: "tennis"
        case .pickleball: "pickleball"
        case .golf: "golf"
        case .soccer: "soccer"
        case .basketball: "basketball"
        case .paddleSports: "paddleSports"
        case .surfingSports: "surfingSports"
        default: "other"
        }
    }
}

struct HealthChanges {
    var readings: [HealthReading]
    /// Each deleted sample's UUID and HealthKit type.
    var deleted: [(UUID, String)]
    var anchors: [String: Data]
}
