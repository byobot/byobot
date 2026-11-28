// How plan and log values read in the app.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import Foundation

enum Format {
    /// `Reps 5 · Load 100 kg · RPE 8`, in registry order.
    static func metrics(_ metrics: Metrics?) -> String {
        guard let metrics, !metrics.isEmpty else { return "Done" }
        return MetricRegistry.sorted(metrics.keys)
            .map { "\(MetricSpec.label(for: $0)) \(metrics[$0]!.display)" }
            .joined(separator: " · ")
    }

    /// `1:30`, or `12:05` past ten minutes.
    static func clock(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds.rounded(.up)))
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    /// `52 min`, or `1 h 10 min`.
    static func duration(_ seconds: TimeInterval) -> String {
        let minutes = Int((seconds / 60).rounded())
        return minutes >= 60 ? "\(minutes / 60) h \(minutes % 60) min" : "\(minutes) min"
    }

    /// `Monday 23 Nov`, or `Today`.
    static func day(_ date: String) -> String {
        if date == LocalDate.today() { return "Today" }
        if date == LocalDate.adding(days: 1) { return "Tomorrow" }
        guard let d = LocalDate.date(date) else { return date }
        return d.formatted(.dateTime.weekday(.wide).day().month(.abbreviated))
    }

    /// `5 min ago`, or `Never`.
    static func ago(_ timestamp: String?) -> String {
        guard let timestamp, let date = Timestamp.date(timestamp) else { return "Never" }
        return date.formatted(.relative(presentation: .named))
    }
}
