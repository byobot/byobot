// Body mass from Apple Health as byobot telemetry. A Hume scale (or any other) writes its weigh-ins
// to Apple Health; the app reads them there and sends them to the laptop.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// One body-mass sample as HealthKit reports it, without depending on HealthKit (so it can be tested).
public struct BodyMassReading: Equatable, Sendable {
    public var uuid: UUID
    public var start: Date
    public var end: Date
    public var kilograms: Double
    /// The app that wrote it to Apple Health, for example the scale maker's app.
    public var sourceName: String
    public var sourceBundleId: String
    public var deviceName: String?
    public var deviceManufacturer: String?
    public var deviceModel: String?
    /// HealthKit's local or UDI identifier for the device, when it has one.
    public var deviceIdentifier: String?

    public init(uuid: UUID, start: Date, end: Date, kilograms: Double, sourceName: String, sourceBundleId: String,
                deviceName: String? = nil, deviceManufacturer: String? = nil, deviceModel: String? = nil,
                deviceIdentifier: String? = nil) {
        self.uuid = uuid
        self.start = start
        self.end = end
        self.kilograms = kilograms
        self.sourceName = sourceName
        self.sourceBundleId = sourceBundleId
        self.deviceName = deviceName
        self.deviceManufacturer = deviceManufacturer
        self.deviceModel = deviceModel
        self.deviceIdentifier = deviceIdentifier
    }
}

public enum HealthMapping {
    /// The telemetry source for anything read from Apple Health. The device that measured it is kept on the sample.
    public static let source = "apple-health"

    /// A body-mass telemetry sample, keyed on HealthKit's sample UUID so sending it again changes nothing.
    /// Readings the contract would refuse (zero, negative, absurd) are skipped.
    public static func sample(_ r: BodyMassReading) -> TelemetrySample? {
        guard r.kilograms > 0, r.kilograms <= 700, r.kilograms.isFinite, r.end >= r.start else { return nil }
        let deviceId = [r.deviceIdentifier, r.sourceBundleId].compactMap { $0 }.first { !$0.isEmpty }
        let device = deviceId.map {
            TelemetryDevice(id: String($0.prefix(200)), name: clip(r.deviceName ?? r.sourceName),
                            manufacturer: clip(r.deviceManufacturer), model: clip(r.deviceModel))
        }
        return TelemetrySample(source: source, sourceId: r.uuid.uuidString.lowercased(), type: .bodyMass,
                               start: Timestamp.string(r.start), end: Timestamp.string(r.end),
                               value: .number((r.kilograms * 1000).rounded() / 1000), unit: "kg", device: device)
    }

    static func clip(_ text: String?) -> String? {
        guard let text, !text.isEmpty else { return nil }
        return String(text.prefix(200))
    }
}
