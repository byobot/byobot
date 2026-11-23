// One session: the planned blocks, rounds and efforts, and what you've logged against each.
// Works with no network; what you log waits on the phone until it can reach your laptop.
// One row per planned effort per round: tap to log or edit, swipe to undo.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct SessionView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    /// The planned session, or nil for an unplanned one.
    let plannedId: String?
    @State private var logging: LogTarget?
    @State private var confirmFinish = false

    private var planned: PlannedSession? {
        plannedId.flatMap { id in model.plan?.sessions.first { $0.id == id } }
    }

    private var active: PerformedSession? { model.book.activeSession }

    /// This screen's session is the one running.
    private var running: Bool {
        guard let active else { return false }
        return active.plannedSessionId == plannedId
    }

    var body: some View {
        List {
            if let planned {
                if let notes = planned.notes { Section { Text(notes) } }
                if let metrics = planned.metrics { Section { Text(Format.metrics(metrics)).foregroundStyle(.secondary) } }
                let logged = model.book.logged(for: planned)
                ForEach(planned.blocks) { block in
                    Section {
                        ForEach(block.efforts) { effort in
                            ForEach(1...max(1, block.rounds ?? 1), id: \.self) { round in
                                row(effort: effort, round: round, rounds: block.rounds ?? 1,
                                    logged: logged[effort.id]?[round])
                            }
                        }
                    } header: {
                        BlockHeader(block: block)
                    }
                }
            }
            if running, let active {
                let extra = model.book.efforts(inSession: active.id).filter { $0.plannedEffortId == nil }
                if !extra.isEmpty || plannedId == nil {
                    Section(plannedId == nil ? "Logged" : "Also logged") {
                        ForEach(extra) { effort in
                            Button {
                                logging = LogTarget(planned: nil, round: nil, editing: effort)
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
            }
            Section {
                if running || active == nil { GymMenu() }
                if running {
                    Button("Log something else") { logging = LogTarget(planned: nil, round: nil, editing: nil) }
                    Button("Finish session") { confirmFinish = true }
                } else if active != nil {
                    Text("Finish the session you're in before starting another.").foregroundStyle(.secondary)
                } else {
                    Button(planned == nil ? "Start a session" : "Start") {
                        model.start(planned, name: planned == nil ? "Session" : nil)
                    }
                }
            }
        }
        .navigationTitle(planned?.name ?? active?.name ?? "Session")
        .sheet(item: $logging) { target in
            NavigationStack {
                LogEffortView(planned: target.planned, round: target.round,
                              activity: target.planned.flatMap { model.plan?.activity($0.activityId) },
                              previous: previous(target), editing: target.editing)
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

    @ViewBuilder
    private func row(effort: PlannedEffort, round: Int, rounds: Int, logged: PerformedEffort?) -> some View {
        let title = activityName(effort.activityId) + (effort.side.map { " (\($0.rawValue))" } ?? "")
            + (rounds > 1 ? ", round \(round)" : "")
        Button {
            logging = LogTarget(planned: effort, round: round, editing: logged)
        } label: {
            HStack {
                Image(systemName: logged == nil ? "circle" : "checkmark.circle.fill")
                    .foregroundStyle(logged == nil ? Color.secondary : Color.green)
                VStack(alignment: .leading) {
                    Text(title).foregroundStyle(.primary)
                    Text(logged.map { Format.metrics($0.metrics) } ?? Format.metrics(effort.metrics))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .disabled(logged == nil && !running)
        .swipeActions {
            if let logged, model.book.canUndo(logged.id) {
                Button(model.book.loggedElsewhere(logged.id) ? "Undo edit" : "Undo", role: .destructive) {
                    model.undo(logged)
                }
            }
        }
    }

    private func activityName(_ id: String) -> String {
        model.plan?.activity(id)?.name ?? id
    }

    /// What you logged for the same effort in the round before, to start the next round from.
    private func previous(_ target: LogTarget) -> PerformedEffort? {
        guard target.editing == nil, let planned, let effort = target.planned, let round = target.round, round > 1 else {
            return nil
        }
        return model.book.logged(for: planned)[effort.id]?[round - 1]
    }
}

struct LogTarget: Identifiable {
    var planned: PlannedEffort?
    var round: Int?
    var editing: PerformedEffort?
    var id: String { "\(planned?.id ?? "-"):\(round ?? 0):\(editing?.id ?? "-")" }
}

private struct BlockHeader: View {
    let block: Block

    var body: some View {
        let parts = [block.name, (block.rounds ?? 1) > 1 ? "\(block.rounds!) rounds" : nil,
                     block.metrics.map { Format.metrics($0) }].compactMap { $0 }
        Text(parts.isEmpty ? "Block" : parts.joined(separator: " · "))
    }
}
