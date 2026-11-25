// The planned sessions your laptop sent, by day; pull down to sync.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct PlanView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        List {
            if let active = model.book.activeSession {
                Section {
                    NavigationLink {
                        if let id = active.plannedSessionId {
                            WorkoutView(plannedId: id)
                        } else {
                            SessionView(plannedId: nil)
                        }
                    } label: {
                        Label("Continue \(active.name ?? "session")", systemImage: "play.circle.fill")
                    }
                }
            }
            if let plan = model.plan {
                if plan.plannedSessions.isEmpty {
                    Text("Nothing planned from \(Format.day(plan.from)) to \(Format.day(plan.to)). Ask your coach for a plan, then pull down to sync.")
                        .foregroundStyle(.secondary)
                }
                ForEach(plan.days, id: \.date) { day in
                    Section(Format.day(day.date)) {
                        ForEach(day.sessions) { session in
                            NavigationLink {
                                WorkoutView(plannedId: session.id)
                            } label: {
                                SessionRow(session: session, done: done(session))
                            }
                        }
                    }
                }
            } else {
                Text("Pull down to get your plan from your laptop.")
                    .foregroundStyle(.secondary)
            }
            if model.book.activeSession == nil {
                Section {
                    NavigationLink {
                        SessionView(plannedId: nil)
                    } label: {
                        Label("Something unplanned", systemImage: "plus.circle")
                    }
                }
            }
        }
        .refreshable { await model.sync() }
        .navigationTitle("Your week")
        .toolbar {
            if model.syncing { ToolbarItem { ProgressView() } }
        }
    }

    private func done(_ session: PlannedSession) -> Int {
        model.book.logged(for: session).values.reduce(0) { $0 + $1.count }
    }
}

private struct SessionRow: View {
    let session: PlannedSession
    let done: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(session.name).font(.headline)
            let total = session.plannedCount
            Text(done == 0 ? "\(total) to do" : "\(min(done, total)) of \(total) done")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }
}
