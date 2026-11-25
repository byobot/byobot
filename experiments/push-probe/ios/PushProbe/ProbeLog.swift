// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation
import UIKit

struct ReceivedPush: Identifiable {
    let id = UUID()
    let pushId: String
    let latency: Double?
    let state: String
    let locked: Bool
}

/// Appends one JSON object per line to a file on the device, excluded from backups.
@MainActor
final class ProbeLog: ObservableObject {
    static let shared = ProbeLog()
    static let conditions = ["normal", "low_power", "swiped_away"]

    let url: URL
    private let launchedAt = Date().timeIntervalSince1970
    @Published private(set) var pushes: [ReceivedPush] = []
    @Published private(set) var token: String?
    @Published private(set) var tokenError: String?
    @Published private(set) var condition: String

    private init() {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var url = dir.appendingPathComponent("push-probe.jsonl")
        if !FileManager.default.fileExists(atPath: url.path) {
            // Writable while locked, so pushes that arrive in a pocket are still logged.
            FileManager.default.createFile(
                atPath: url.path, contents: nil,
                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        }
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? url.setResourceValues(values)
        self.url = url
        condition = UserDefaults.standard.string(forKey: "condition") ?? "normal"
        load()
    }

    static func stateName(_ state: UIApplication.State) -> String {
        switch state {
        case .active: return "active"
        case .inactive: return "inactive"
        case .background: return "background"
        @unknown default: return "unknown"
        }
    }

    func setCondition(_ value: String) {
        condition = value
        UserDefaults.standard.set(value, forKey: "condition")
        append(["kind": "mark", "condition": value])
    }

    func launched(byPush: Bool, state: UIApplication.State) {
        append(["kind": "launch", "by_push": byPush, "app_state": Self.stateName(state)])
        // Opening the app again ends a swiped-away stretch.
        if condition == "swiped_away" && state != .background { setCondition("normal") }
    }

    func phase(_ name: String) {
        append(["kind": name])
    }

    func setToken(_ value: String?, error: String? = nil) {
        token = value
        tokenError = error
    }

    func recordPush(_ userInfo: [AnyHashable: Any], application: UIApplication) {
        let now = Date().timeIntervalSince1970
        let probe = userInfo["probe"] as? [String: Any]
        let device = UIDevice.current
        device.isBatteryMonitoringEnabled = true
        var entry: [String: Any] = [
            "kind": "push",
            "at": now,
            "app_state": Self.stateName(application.applicationState),
            "protected_data": application.isProtectedDataAvailable,
            "low_power": ProcessInfo.processInfo.isLowPowerModeEnabled,
            "battery": device.batteryLevel,
            "uptime": now - launchedAt,
            "condition": condition,
        ]
        if let id = probe?["id"] as? String { entry["id"] = id }
        if let sent = probe?["sent_at"] as? Double { entry["sent_at"] = sent }
        let remaining = application.backgroundTimeRemaining
        if remaining < 1e6 { entry["time_remaining"] = remaining }
        append(entry)
    }

    private func append(_ entry: [String: Any]) {
        var entry = entry
        if entry["at"] == nil { entry["at"] = Date().timeIntervalSince1970 }
        guard let data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]),
              let handle = try? FileHandle(forWritingTo: url) else { return }
        handle.seekToEndOfFile()
        handle.write(data + Data([0x0a]))
        try? handle.close()
        if let push = Self.push(from: entry) { pushes.insert(push, at: 0) }
    }

    private func load() {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return }
        pushes = text.split(separator: "\n").reversed().compactMap { line in
            guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else { return nil }
            return Self.push(from: obj)
        }
    }

    private static func push(from entry: [String: Any]) -> ReceivedPush? {
        guard entry["kind"] as? String == "push" else { return nil }
        let at = entry["at"] as? Double
        let sent = entry["sent_at"] as? Double
        return ReceivedPush(
            pushId: entry["id"] as? String ?? "?",
            latency: at.flatMap { a in sent.map { a - $0 } },
            state: entry["app_state"] as? String ?? "unknown",
            locked: !(entry["protected_data"] as? Bool ?? true))
    }
}
