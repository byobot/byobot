// A session with no plan, or one whose plan is gone: what you've logged, tap to edit, swipe to undo.
// Works with no network; what you log waits on the phone until it can reach your laptop.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct SessionView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    /// The planned session this screen continues, or nil for an unplanned one.
    let plannedId: String?
    @State private var logging: LogTarget?
    @State private var confirmFinish = false

    private var active: PerformedSession? { model.book.activeSession }

    /// This screen's session is the one running.
    private var running: Bool {
        guard let active else { return false }
        return active.plannedSessionId == plannedId
    }

    var body: some View {
        List {
            if running, let active {
                Section("Logged") {
                    ForEach(model.book.efforts(inSession: active.id)) { effort in
                        Button {
                            logging = LogTarget(editing: effort)
                        } label: {
                            VStack(alignment: .leading) {
                                Text(activityName(effort.activityId)).foregroundStyle(.primary)
                                Text(Format.metrics(effort.metrics)).font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                        .swipeActions { Button("Undo", role: .destructive) { model.undo(effort) } }
                    }
                }
            }
            Section {
                if running || active == nil { GymMenu() }
                if running {
                    Button("Log something else") { logging = LogTarget(editing: nil) }
                    Button("Finish session") { confirmFinish = true }
                } else if active != nil {
                    Text("Finish the session you're in before starting another.").foregroundStyle(.secondary)
                } else {
                    Button("Start a session") { model.start(nil, name: "Session") }
                }
            }
        }
        .navigationTitle(active?.name ?? "Session")
        .sheet(item: $logging) { target in
            NavigationStack {
                LogEffortView(planned: nil, round: nil, activity: nil, editing: target.editing)
            }
            .environmentObject(model)
        }
        .confirmationDialog("Finish this session?", isPresented: $confirmFinish, titleVisibility: .visible) {
            Button("Finish") {
                model.finish()
                dismiss()
            }
        }
    }

    private func activityName(_ id: String) -> String {
        model.book.activity(id)?.name ?? id
    }
}

struct LogTarget: Identifiable {
    var editing: PerformedEffort?
    var id: String { editing?.id ?? "-" }
}
