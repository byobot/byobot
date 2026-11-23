// What a session came to: each exercise's plan against what you did, and anything extra.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct SummaryView: View {
    @Environment(\.dismiss) private var dismiss
    let summary: SessionSummary
    let names: (String) -> String

    var body: some View {
        List {
            Section {
                HStack {
                    Stat(value: "\(summary.done)/\(summary.total)", label: "sets")
                    if let duration = summary.duration {
                        Stat(value: Format.duration(duration), label: "time")
                    }
                }
            }
            ForEach(summary.rows) { row in
                Section {
                    LabeledContent("Plan", value: (row.exercise.total > 1 ? "\(row.exercise.total) × " : "")
                                   + Format.metrics(row.exercise.effort.metrics))
                    if row.sets.isEmpty {
                        Text("Skipped").foregroundStyle(.secondary)
                    }
                    ForEach(row.sets) { set in
                        Text(Format.metrics(set.metrics)).font(.subheadline)
                    }
                } header: {
                    Text(names(row.exercise.activityId)
                         + (row.exercise.swap != nil ? " (in place of \(names(row.exercise.effort.activityId)))" : ""))
                }
            }
            if !summary.extras.isEmpty {
                Section("Also done") {
                    ForEach(summary.extras) { effort in
                        LabeledContent(names(effort.activityId), value: Format.metrics(effort.metrics))
                    }
                }
            }
        }
        .navigationTitle("Done")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Close") { dismiss() } } }
    }
}

private struct Stat: View {
    let value: String
    let label: String

    var body: some View {
        VStack {
            Text(value).font(.title.bold()).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}
