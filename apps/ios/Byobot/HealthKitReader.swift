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

    /// Quantity types with the HealthKit unit to read them in, and the factor to the contract's unit.
    private let quantities: [(HKQuantityTypeIdentifier, TelemetryType, HKUnit, Double)] = [
        (.bodyMass, .bodyMass, .gramUnit(with: .kilo), 1),
        (.leanBodyMass, .leanBodyMass, .gramUnit(with: .kilo), 1),
        (.bodyFatPercentage, .bodyFatPercentage, .percent(), 100),
        (.bodyMassIndex, .bodyMassIndex, .count(), 1),
        (.height, .height, .meter(), 1),
        (.waistCircumference, .waistCircumference, .meter(), 1),
        (.vo2Max, .vo2Max, HKUnit(from: "ml/kg*min"), 1),
        (.heartRateVariabilitySDNN, .heartRateVariabilitySDNN, .secondUnit(with: .milli), 1),
        (.restingHeartRate, .restingHeartRate, HealthKitReader.bpm, 1),
        (.walkingHeartRateAverage, .walkingHeartRateAverage, HealthKitReader.bpm, 1),
        (.heartRateRecoveryOneMinute, .heartRateRecoveryOneMinute, HealthKitReader.bpm, 1),
        (.respiratoryRate, .respiratoryRate, HealthKitReader.bpm, 1),
        (.oxygenSaturation, .oxygenSaturation, .percent(), 100),
        (.walkingSpeed, .walkingSpeed, HealthKitReader.metersPerSecond, 1),
        (.walkingStepLength, .walkingStepLength, .meter(), 1),
        (.walkingDoubleSupportPercentage, .walkingDoubleSupportPercentage, .percent(), 100),
        (.walkingAsymmetryPercentage, .walkingAsymmetryPercentage, .percent(), 100),
        (.appleWalkingSteadiness, .appleWalkingSteadiness, .percent(), 100),
        (.sixMinuteWalkTestDistance, .sixMinuteWalkTestDistance, .meter(), 1),
        (.stairAscentSpeed, .stairAscentSpeed, HealthKitReader.metersPerSecond, 1),
        (.stairDescentSpeed, .stairDescentSpeed, HealthKitReader.metersPerSecond, 1),
        (.bloodPressureSystolic, .bloodPressureSystolic, .millimeterOfMercury(), 1),
        (.bloodPressureDiastolic, .bloodPressureDiastolic, .millimeterOfMercury(), 1),
        (.bloodGlucose, .bloodGlucose, HKUnit(from: "mg/dL"), 1),
        (.bodyTemperature, .bodyTemperature, .degreeCelsius(), 1),
        (.appleSleepingWristTemperature, .appleSleepingWristTemperature, .degreeCelsius(), 1),
    ]
    /// Read as one total per day across every app and device, since the phone and watch both count steps.
    private let totals: [(HKQuantityTypeIdentifier, TelemetryType, HKUnit)] = [
        (.stepCount, .stepCount, .count()),
        (.flightsClimbed, .flightsClimbed, .count()),
        (.distanceWalkingRunning, .distanceWalkingRunning, .meter()),
        (.distanceCycling, .distanceCycling, .meter()),
        (.distanceSwimming, .distanceSwimming, .meter()),
        (.activeEnergyBurned, .activeEnergyBurned, .kilocalorie()),
        (.basalEnergyBurned, .basalEnergyBurned, .kilocalorie()),
        (.appleExerciseTime, .appleExerciseTime, .second()),
        (.appleStandTime, .appleStandTime, .second()),
    ]
    private static var bpm: HKUnit { .count().unitDivided(by: .minute()) }
    private static var metersPerSecond: HKUnit { .meter().unitDivided(by: .second()) }
    private let sleep = HKCategoryType(.sleepAnalysis)
    private let limit = 50_000

    var available: Bool { HKHealthStore.isHealthDataAvailable() }

    private var readTypes: Set<HKObjectType> {
        var types: Set<HKObjectType> = [sleep, HKObjectType.workoutType(), HKQuantityType(.heartRate),
                                        HKQuantityType(.activeEnergyBurned)]
        for (id, _, _, _) in quantities { types.insert(HKQuantityType(id)) }
        for (id, _, _) in totals { types.insert(HKQuantityType(id)) }
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

    func readings(since: Date) async throws -> [HealthReading] {
        let window = HKQuery.predicateForSamples(withStart: since, end: nil)
        var out: [HealthReading] = []
        for (id, type, unit, factor) in quantities {
            let query = HKSampleQueryDescriptor(
                predicates: [.quantitySample(type: HKQuantityType(id), predicate: window)],
                sortDescriptors: [SortDescriptor(\.startDate)], limit: limit)
            let samples = try await query.result(for: store)
            out += samples.map { reading($0, .quantity(type, $0.quantity.doubleValue(for: unit) * factor)) }
        }
        let sleeps = HKSampleQueryDescriptor(predicates: [.categorySample(type: sleep, predicate: window)],
                                             sortDescriptors: [SortDescriptor(\.startDate)], limit: limit)
        let nights = try await sleeps.result(for: store)
        out += nights.map { reading($0, .sleep($0.value)) }
        let workouts = HKSampleQueryDescriptor(predicates: [.workout(window)],
                                               sortDescriptors: [SortDescriptor(\.startDate)], limit: limit)
        let sessions = try await workouts.result(for: store)
        out += sessions.map { reading($0, .workout(Self.name($0.workoutActivityType), workoutTotals($0))) }
        return out
    }

    /// Each day's totals, and its average, lowest and highest heart rate, from the start of the day `since` falls in.
    func dailyTotals(since: Date) async throws -> [DailyTotal] {
        let start = Calendar.current.startOfDay(for: since)
        let window = HKQuery.predicateForSamples(withStart: start, end: nil)
        let day = DateComponents(day: 1)
        var out: [DailyTotal] = []
        for (id, type, unit) in totals {
            let query = HKStatisticsCollectionQueryDescriptor(
                predicate: .quantitySample(type: HKQuantityType(id), predicate: window),
                options: .cumulativeSum, anchorDate: start, intervalComponents: day)
            let days = try await query.result(for: store)
            for stats in days.statistics() {
                if let sum = stats.sumQuantity() {
                    out.append(DailyTotal(type: type, start: stats.startDate, end: stats.endDate,
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
                out.append(DailyTotal(type: .heartRate, start: stats.startDate, end: stats.endDate,
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
