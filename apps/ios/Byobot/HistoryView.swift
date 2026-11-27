// Sessions you finished on this phone, newest first, and what you did in each.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct HistoryView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        List {
            if model.book.finishedSessions.isEmpty {
                Text("Sessions you finish show up here.").foregroundStyle(.secondary)
            }
            ForEach(model.book.finishedSessions) { session in
                NavigationLink {
                    HistoryDetail(session: session)
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(session.name ?? "Session").font(.headline)
                        Text(line(session)).font(.subheadline).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .navigationTitle("History")
    }

    private func line(_ session: PerformedSession) -> String {
        var parts: [String] = []
        if let start = Timestamp.date(session.startedAt) {
            parts.append(start.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated)))
            if let end = session.endedAt.flatMap(Timestamp.date) {
                parts.append(Format.duration(end.timeIntervalSince(start)))
            }
        }
        parts.append("\(model.book.efforts(inSession: session.id).count) sets")
        return parts.joined(separator: " · ")
    }
}

private struct HistoryDetail: View {
    @EnvironmentObject var model: AppModel
    let session: PerformedSession

    var body: some View {
        List {
            if let notes = session.notes { Section { Text(notes) } }
            Section {
                ForEach(model.book.efforts(inSession: session.id)) { effort in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(model.book.activity(effort.activityId)?.name ?? effort.activityId)
                        Text(Format.metrics(effort.metrics)).font(.subheadline).foregroundStyle(.secondary)
                        if let notes = effort.notes {
                            Text(notes).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle(session.name ?? "Session")
    }
}
