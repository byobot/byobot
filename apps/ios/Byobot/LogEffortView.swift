// Logging one effort: a set, an interval, a hold. The fields come from the plan's targets and the
// activity's usual metrics; any registry metric or a custom `x.` one can be added.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct LogEffortView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let planned: PlannedEffort?
    let round: Int?
    let activity: Activity?
    let editing: PerformedEffort?
    /// Logs the set some other way, such as against a swap.
    let onSave: ((Metrics, Side?, String) -> Void)?
    @State private var activityText: String
    @State private var fields: [MetricField]
    @State private var side: Side?
    @State private var notes = ""
    @State private var customName = ""
    @State private var error: String?
    @State private var performedAt: Date

    init(planned: PlannedEffort?, round: Int?, activity: Activity?,
         editing: PerformedEffort? = nil, prefill: Metrics? = nil, onSave: ((Metrics, Side?, String) -> Void)? = nil) {
        self.planned = planned
        self.onSave = onSave
        self.round = round
        self.activity = activity
        self.editing = editing
        _performedAt = State(initialValue: editing.flatMap { Timestamp.date($0.performedAt) } ?? Date())
        if let editing {
            _fields = State(initialValue: EffortForm.fields(planned: planned, activity: activity, filledFrom: editing.metrics))
            _activityText = State(initialValue: editing.activityId)
            _side = State(initialValue: editing.side)
            _notes = State(initialValue: editing.notes ?? "")
            return
        }
        var fields = prefill.map { EffortForm.fields(planned: planned, activity: activity, filledFrom: $0) }
            ?? EffortForm.fields(planned: planned, activity: activity)
        if planned == nil && fields.isEmpty {
            fields = ["reps", "load"].map { EffortForm.field(name: $0, target: nil) }
        }
        _fields = State(initialValue: fields)
        _activityText = State(initialValue: planned?.activityId ?? "")
        _side = State(initialValue: planned?.side)
    }

    var body: some View {
        Form {
            Section {
                if let planned {
                    LabeledContent("Activity", value: activity?.name ?? planned.activityId)
                    if let round { LabeledContent("Round", value: String(round)) }
                } else {
                    TextField("Activity, e.g. pull_up", text: $activityText)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
            }
            Section("What you did") {
                ForEach($fields) { $field in
                    FieldRow(field: $field)
                }
                .onDelete { fields.remove(atOffsets: $0) }
                Menu("Add a measure") {
                    ForEach(MetricRegistry.all.filter { spec in !fields.contains { $0.name == spec.name } }, id: \.name) { spec in
                        Button(MetricSpec.label(for: spec.name)) {
                            fields.append(EffortForm.field(name: spec.name, target: nil))
                        }
                    }
                }
                HStack {
                    TextField("Your own, e.g. x.grade", text: $customName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("Add") {
                        fields.append(EffortForm.field(name: customName, target: nil))
                        customName = ""
                    }
                    .disabled(!MetricRegistry.isCustom(customName) || fields.contains { $0.name == customName })
                }
            }
            Section {
                Picker("Side", selection: $side) {
                    Text("Not one side").tag(Side?.none)
                    ForEach(Side.allCases, id: \.self) { Text($0.rawValue.capitalized).tag(Side?.some($0)) }
                }
                TextField("Notes", text: $notes, axis: .vertical)
                if editing != nil {
                    DatePicker("When", selection: $performedAt)
                }
            }
            if let error {
                Section { Text(error).foregroundStyle(.red) }
            }
        }
        .navigationTitle(editing == nil ? "Log" : "Edit")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) { Button("Save") { save() } }
        }
    }

    private func save() {
        let activityId = planned?.activityId ?? ActivityId.make(activityText, known: model.plan?.activities ?? [])
        guard !activityId.isEmpty else {
            error = "Name the activity."
            return
        }
        do {
            let metrics = try EffortForm.metrics(from: fields)
            let notes = self.notes.trimmingCharacters(in: .whitespacesAndNewlines)
            if let editing {
                let moved = Timestamp.date(editing.performedAt).map { abs($0.timeIntervalSince(performedAt)) >= 1 } ?? true
                model.edit(editing, metrics: metrics, side: side, notes: notes, activityId: planned == nil ? activityId : nil,
                           performedAt: moved ? Timestamp.string(performedAt) : nil)
                dismiss()
                return
            }
            if let onSave {
                onSave(metrics, side, notes)
            } else {
                model.log(activityId: activityId, metrics: metrics, planned: planned, round: round, side: side, notes: notes)
            }
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct FieldRow: View {
    @Binding var field: MetricField

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(field.label)
                Spacer()
                TextField(field.target?.display ?? "", text: $field.text)
                    .multilineTextAlignment(.trailing)
                    .keyboardType(field.spec?.kind == .text || field.spec == nil ? .default : .decimalPad)
                    .frame(maxWidth: 120)
                if field.units.count > 1 {
                    Picker("Unit", selection: $field.unit) {
                        ForEach(field.units, id: \.self) { Text(UnitLabel.text($0)).tag(String?.some($0)) }
                    }
                    .labelsHidden()
                    .fixedSize()
                } else if let unit = field.unit {
                    Text(UnitLabel.text(unit)).foregroundStyle(.secondary)
                }
            }
            if let target = field.target {
                Text("Plan: \(target.display)").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
