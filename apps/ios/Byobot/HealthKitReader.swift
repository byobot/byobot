// Reading body mass from Apple Health. A Hume scale (or any scale) writes weigh-ins there through
// its own app; byobot only reads them, and never writes to Apple Health.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import Foundation
import HealthKit

final class HealthKitReader: @unchecked Sendable {
    private let store = HKHealthStore()
    private let bodyMass = HKQuantityType(.bodyMass)

    var available: Bool { HKHealthStore.isHealthDataAvailable() }

    /// Ask to read body mass. iOS doesn't tell apps whether reading was allowed, so a refusal just reads as no data.
    func requestAccess() async throws {
        try await store.requestAuthorization(toShare: [], read: [bodyMass])
    }

    func bodyMass(since: Date) async throws -> [BodyMassReading] {
        let predicate = HKSamplePredicate.quantitySample(
            type: bodyMass, predicate: HKQuery.predicateForSamples(withStart: since, end: nil))
        let query = HKSampleQueryDescriptor(predicates: [predicate], sortDescriptors: [SortDescriptor(\.startDate)],
                                            limit: 10_000)
        let kg = HKUnit.gramUnit(with: .kilo)
        return try await query.result(for: store).map { sample in
            BodyMassReading(uuid: sample.uuid, start: sample.startDate, end: sample.endDate,
                            kilograms: sample.quantity.doubleValue(for: kg),
                            sourceName: sample.sourceRevision.source.name,
                            sourceBundleId: sample.sourceRevision.source.bundleIdentifier,
                            deviceName: sample.device?.name, deviceManufacturer: sample.device?.manufacturer,
                            deviceModel: sample.device?.model,
                            deviceIdentifier: sample.device?.localIdentifier ?? sample.device?.udiDeviceIdentifier)
        }
    }
}
