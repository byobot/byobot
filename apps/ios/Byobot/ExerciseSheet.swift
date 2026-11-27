// One exercise of a planned session: its plan, the sets you've done, and swaps that work the same
// muscles with the equipment you have.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct ExerciseSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let planned: PlannedSession
    let effortId: String
    @State private var reason = ""
    @State private var editing: LogForm?

    private static let reasons = ["Equipment taken", "Doesn't feel right", "Not today"]

    var body: some View {
        let workout = model.book.workout(planned)
        let exercise = workout.exercises.first { $0.id == effortId }
        NavigationStack {
            List {
                if let exercise {
                    about(exercise, workout)
                    done(workout)
                    swaps(exercise, workout)
                }
            }
            .navigationTitle(exercise.map { workout.name($0.activityId) } ?? "Exercise")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .sheet(item: $editing) { form in
                NavigationStack {
                    LogEffortView(planned: form.slot.effort, round: form.slot.round,
                                  activity: model.book.activity(form.slot.activityId), editing: form.editing)
                }
                .environmentObject(model)
            }
        }
    }

    @ViewBuilder
    private func about(_ exercise: Exercise, _ workout: Workout) -> some View {
        Section {
            LabeledContent("Plan", value: (exercise.total > 1 ? "\(exercise.total) × " : "")
                           + Format.metrics(exercise.effort.metrics))
            if exercise.swap != nil {
                LabeledContent("In place of", value: workout.name(exercise.effort.activityId))
            }
            if let targets = workout.activity(exercise.activityId)?.targets {
                LabeledContent("Works", value: Self.describe(targets))
            }
            if let notes = exercise.effort.notes { Text(notes).foregroundStyle(.secondary) }
        }
    }

    @ViewBuilder
    private func done(_ workout: Workout) -> some View {
        let slots = workout.slots.filter { $0.effort.id == effortId && $0.logged != nil }
        if !slots.isEmpty {
            Section("Done") {
                ForEach(slots) { slot in
                    if let effort = slot.logged {
                        Button {
                            editing = LogForm(slot: slot, values: effort.metrics ?? [:], editing: effort)
                        } label: {
                            VStack(alignment: .leading) {
                                Text("Set \(slot.round)").foregroundStyle(.primary)
                                Text(Format.metrics(effort.metrics)).font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                        .swipeActions {
                            if model.book.canUndo(effort.id) {
                                Button("Undo", role: .destructive) { model.undo(effort) }
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func swaps(_ exercise: Exercise, _ workout: Workout) -> some View {
        let alternatives = model.book.alternatives(for: exercise.effort)
        Section {
            if alternatives.isEmpty {
                Text("No swaps for this one: no other activity works the same muscles.")
                    .foregroundStyle(.secondary)
            } else {
                TextField("Why? (optional)", text: $reason)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        ForEach(Self.reasons, id: \.self) { text in
                            Button(text) { reason = text }
                                .buttonStyle(.bordered)
                                .font(.caption)
                        }
                    }
                }
                ForEach(alternatives) { alternative in
                    Button {
                        let why = reason.trimmingCharacters(in: .whitespacesAndNewlines)
                        model.swap(effortId, to: Swap(activityId: alternative.id, reason: why.isEmpty ? nil : why),
                                   for: planned)
                        dismiss()
                    } label: {
                        AlternativeRow(alternative: alternative, current: alternative.id == exercise.activityId)
                    }
                }
            }
        } header: {
            Text("Swap for")
        } footer: {
            if !alternatives.isEmpty {
                Text("Ranked by the muscles they share with \(workout.name(exercise.effort.activityId)), ones you have the equipment for first. Your coach sees the swap and why.")
            }
        }
        if exercise.swap != nil {
            Section {
                Button("Back to \(workout.name(exercise.effort.activityId))") {
                    model.swap(effortId, to: nil, for: planned)
                    dismiss()
                }
            }
        }
    }

    static func describe(_ targets: Targets) -> String {
        let main = (targets.main ?? []).joined(separator: ", ")
        let synergists = (targets.synergists ?? []).joined(separator: ", ")
        return synergists.isEmpty ? main : "\(main); also \(synergists)"
    }
}

private struct AlternativeRow: View {
    let alternative: Alternative
    let current: Bool

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(alternative.activity.name).foregroundStyle(.primary)
                if let targets = alternative.activity.targets {
                    Text(ExerciseSheet.describe(targets)).font(.caption).foregroundStyle(.secondary)
                }
                if !alternative.missingEquipment.isEmpty {
                    Text("Needs " + alternative.missingEquipment.joined(separator: ", "))
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
            Spacer()
            if current { Image(systemName: "checkmark").foregroundStyle(.tint) }
        }
    }
}
