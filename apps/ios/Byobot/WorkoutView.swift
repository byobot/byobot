// A planned session as you do it: the next set up front, rest and interval clocks, and every exercise
// below, to reorder or swap.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import Combine
import SwiftUI

struct WorkoutView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let plannedId: String
    @State private var now = Date()
    /// Values nudged for the slot named in `draftFor`.
    @State private var draft: Metrics = [:]
    @State private var draftFor: String?
    @State private var logForm: LogForm?
    @State private var exercise: Exercise?
    @State private var reordering = false
    @State private var confirmFinish = false
    @State private var summary: SessionSummary?
    @State private var unplanned = false
    private let ticker = Timer.publish(every: 0.25, on: .main, in: .common).autoconnect()

    private var planned: PlannedSession? { model.plan?.plannedSession(plannedId) }
    private var active: PerformedSession? { model.book.activeSession }
    private var running: Bool { active?.plannedSessionId == plannedId }

    var body: some View {
        Group {
            if let planned {
                content(planned, model.book.workout(planned))
            } else {
                ContentUnavailableView("Not in your plan any more", systemImage: "calendar.badge.exclamationmark",
                                       description: Text("Pull down on Week to sync with your laptop."))
            }
        }
        .navigationTitle(planned?.name ?? "Session")
        .navigationBarTitleDisplayMode(.inline)
        .onReceive(ticker) { tick in
            now = tick
            model.tick(now: tick)
        }
        .sheet(item: $logForm) { form in
            NavigationStack {
                LogEffortView(planned: form.slot.effort, round: form.slot.round,
                              activity: model.book.activity(form.slot.activityId),
                              editing: form.editing, prefill: form.editing == nil ? form.values : nil,
                              onSave: saver(form))
            }
            .environmentObject(model)
        }
        .sheet(item: $exercise) { item in
            if let planned {
                ExerciseSheet(planned: planned, effortId: item.id)
                    .environmentObject(model)
            }
        }
        .sheet(item: $summary, onDismiss: { dismiss() }) { summary in
            NavigationStack { SummaryView(summary: summary, names: { model.book.name(of: $0) }) }
        }
        .sheet(isPresented: $unplanned) {
            NavigationStack {
                LogEffortView(planned: nil, round: nil, activity: nil)
            }
            .environmentObject(model)
        }
        .confirmationDialog("Finish this session?", isPresented: $confirmFinish, titleVisibility: .visible) {
            Button("Finish") { finish() }
        }
    }

    @ViewBuilder
    private func content(_ planned: PlannedSession, _ workout: Workout) -> some View {
        List {
            if running {
                Section {
                    focus(planned, workout)
                } header: {
                    gym(planned)
                }
            } else {
                Section {
                    if let notes = planned.notes { Text(notes) }
                    Text("\(workout.exercises.count) exercises · \(workout.total) sets")
                        .foregroundStyle(.secondary)
                    if active == nil {
                        Button {
                            model.start(planned)
                        } label: {
                            Text("Start").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                    } else {
                        Text("Finish the session you're in before starting another.").foregroundStyle(.secondary)
                    }
                }
            }
            Section {
                ForEach(workout.exercises) { item in
                    Button { exercise = item } label: { ExerciseRow(exercise: item, workout: workout) }
                        .swipeActions {
                            Button("Swap") { exercise = item }.tint(.indigo)
                        }
                }
                .onMove { from, to in
                    var ids = workout.exercises.map(\.id)
                    ids.move(fromOffsets: from, toOffset: to)
                    model.setOrder(ids, for: planned)
                }
            } header: {
                HStack {
                    Text("Exercises")
                    Spacer()
                    Button(reordering ? "Done" : "Reorder") { withAnimation { reordering.toggle() } }
                        .font(.subheadline)
                        .textCase(nil)
                }
            } footer: {
                Text("Tap an exercise to see its sets or swap it.")
            }
            if running, let active {
                let extras = model.book.extras(inSession: active.id)
                if !extras.isEmpty {
                    Section("Also logged") {
                        ForEach(extras) { effort in
                            VStack(alignment: .leading) {
                                Text(model.book.name(of: effort.activityId))
                                Text(Format.metrics(effort.metrics)).font(.subheadline).foregroundStyle(.secondary)
                            }
                            .swipeActions { Button("Undo", role: .destructive) { model.undo(effort) } }
                        }
                    }
                }
                Section {
                    Button("Log something else") { unplanned = true }
                    Button("Finish session") { confirmFinish = true }
                }
            }
        }
        .environment(\.editMode, .constant(reordering ? .active : .inactive))
    }

    // MARK: The card on top

    @ViewBuilder
    private func focus(_ planned: PlannedSession, _ workout: Workout) -> some View {
        if let running = model.clock, running.plannedSessionId == planned.id, let timer = model.intervalClock(running) {
            ClockCard(clock: timer, elapsed: now.timeIntervalSince(running.startedAt), workout: workout) {
                model.stopClock()
            }
        } else if let rest = model.rest {
            RestCard(rest: rest, now: now, next: workout.next, workout: workout,
                     onAdd: { model.rest = rest.adding(30) }, onSkip: { model.rest = nil })
        } else if let slot = workout.next {
            let prefill = model.book.asPlanned(slot, in: workout)
            let values = draftFor == slot.id ? draft : prefill
            FocusCard(slot: slot, workout: workout, values: values, asPlanned: values == prefill,
                      lastTime: model.book.lastTime(slot.activityId, excludingSession: active?.id,
                                                    at: model.book.currentLocationId),
                      gymName: { id in GymMenu.name(model.book.location(id), id: id) },
                      clockSlots: workout.clockSlots(from: slot),
                      onNudge: { name, direction in
                          draft = Nudge.apply(values, name, direction)
                          draftFor = slot.id
                      },
                      onLog: {
                          model.log(slot, metrics: values, in: planned)
                          draftFor = nil
                      },
                      onEdit: { logForm = LogForm(slot: slot, values: values, editing: nil) },
                      onClock: { slots in model.startClock(slots, for: planned) })
        } else {
            VStack(alignment: .leading, spacing: 12) {
                Label("Every planned set is done", systemImage: "checkmark.seal.fill")
                    .font(.title3.bold())
                    .foregroundStyle(.green)
                Button {
                    confirmFinish = true
                } label: {
                    Text("Finish session").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
            .padding(.vertical, 8)
        }
    }

    @ViewBuilder
    private func gym(_ planned: PlannedSession) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            GymMenu().font(.subheadline).textCase(nil)
            if model.book.plannedElsewhere(planned), let id = planned.locationId {
                Text("Loads were planned for \(GymMenu.name(model.book.location(id), id: id)).")
                    .font(.caption)
                    .textCase(nil)
            }
        }
    }

    /// Logs a new set from the form against its slot; an edit saves the usual way.
    private func saver(_ form: LogForm) -> ((Metrics, Side?, String) -> Void)? {
        guard form.editing == nil, let planned else { return nil }
        return { metrics, _, notes in model.log(form.slot, metrics: metrics, notes: notes, in: planned) }
    }

    private func finish() {
        guard let planned, let active else { return }
        let extras = model.book.extras(inSession: active.id)
        let performed = model.finish()
        summary = model.book.workout(planned).summary(extras: extras, performed: performed)
    }
}

struct LogForm: Identifiable {
    var slot: Slot
    var values: Metrics
    var editing: PerformedEffort?
    var id: String { slot.id + (editing?.id ?? "") }
}

extension Exercise {
    var swapped: Bool { swap != nil }
}

// MARK: Cards

/// The set to do now: big targets, nudges, and one tap to log it as planned.
private struct FocusCard: View {
    let slot: Slot
    let workout: Workout
    let values: Metrics
    let asPlanned: Bool
    let lastTime: LastTime
    let gymName: (String) -> String
    let clockSlots: [Slot]?
    let onNudge: (String, Int) -> Void
    let onLog: () -> Void
    let onEdit: () -> Void
    let onClock: ([Slot]) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(caption).font(.subheadline).foregroundStyle(.secondary)
                Text(workout.name(slot.activityId)).font(.largeTitle.bold())
                if slot.swap != nil {
                    Text("In place of \(workout.name(slot.effort.activityId))")
                        .font(.subheadline).foregroundStyle(.indigo)
                }
            }
            ForEach(MetricRegistry.sorted(values.keys), id: \.self) { name in
                MetricNudger(name: name, value: values[name]!, target: slot.effort.metrics?[name]) {
                    onNudge(name, $0)
                }
            }
            if let hr = slot.effort.metrics?["heart_rate"] {
                Label("Heart rate \(hr.display)", systemImage: "heart.fill").foregroundStyle(.red)
            }
            if let first = lastTime.efforts.first {
                let sets = lastTime.efforts.count
                let place = lastTime.locationId.map { "at \(gymName($0))" } ?? "gym not recorded"
                Text((lastTime.elsewhere ? "Last time, \(place): " : "Last time: ")
                     + Format.metrics(first.metrics) + (sets > 1 ? " (\(sets) sets)" : ""))
                    .font(.footnote)
                    .foregroundStyle(lastTime.elsewhere ? Color.orange : Color.secondary)
            }
            if let clockSlots {
                Button {
                    onClock(clockSlots)
                } label: {
                    Label(clockSlots.count > 1 ? "Start the clock" : "Start the timer", systemImage: "timer")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
            HStack {
                let log = Button(action: onLog) {
                    Text(asPlanned ? "Done as planned" : "Log set").frame(maxWidth: .infinity)
                }
                .controlSize(.large)
                if clockSlots == nil {
                    log.buttonStyle(.borderedProminent)
                } else {
                    log.buttonStyle(.bordered)
                }
                Button("Edit", action: onEdit)
                    .buttonStyle(.bordered)
                    .controlSize(.large)
            }
        }
        .padding(.vertical, 8)
    }

    private var caption: String {
        var parts = ["Set \(slot.round) of \(slot.rounds)"]
        if let name = slot.block.name { parts.append(name) }
        if let side = slot.effort.side { parts.append(side.rawValue.capitalized) }
        if slot.perSide { parts.append("each side") }
        return parts.joined(separator: " · ")
    }
}

/// One metric, big, with -/+ when it has a step.
private struct MetricNudger: View {
    let name: String
    let value: Measure
    let target: Measure?
    let onNudge: (Int) -> Void

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 0) {
                Text(MetricSpec.label(for: name)).font(.caption).foregroundStyle(.secondary)
                Text(value.display).font(.title.weight(.semibold)).monospacedDigit()
                if let target, target != value {
                    Text("Plan \(target.display)").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if Nudge.step(name, unit: value.unit) != nil, value.value?.number != nil {
                Button { onNudge(-1) } label: { Image(systemName: "minus").frame(width: 28, height: 28) }
                    .buttonStyle(.bordered)
                Button { onNudge(1) } label: { Image(systemName: "plus").frame(width: 28, height: 28) }
                    .buttonStyle(.bordered)
            }
        }
    }
}

private struct RestCard: View {
    let rest: Countdown
    let now: Date
    let next: Slot?
    let workout: Workout
    let onAdd: () -> Void
    let onSkip: () -> Void

    var body: some View {
        VStack(spacing: 12) {
            Text("Rest").font(.headline).foregroundStyle(.secondary)
            Text(Format.clock(rest.remaining(at: now)))
                .font(.system(size: 72, weight: .bold, design: .rounded))
                .monospacedDigit()
            ProgressView(value: rest.seconds - rest.remaining(at: now), total: max(rest.seconds, 1))
            if let next {
                Text("Next: \(workout.name(next.activityId)), set \(next.round) of \(next.rounds)")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button("+30 s", action: onAdd).buttonStyle(.bordered)
                Button("Skip", action: onSkip).buttonStyle(.borderedProminent)
            }
            .controlSize(.large)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
    }
}

/// Work and rest phases for an interval block or a hold, logging each set as its work ends.
private struct ClockCard: View {
    let clock: IntervalClock
    let elapsed: TimeInterval
    let workout: Workout
    let onStop: () -> Void

    var body: some View {
        let position = clock.position(elapsed: elapsed)
        let phase = position.map { clock.phases[$0.index] }
        VStack(spacing: 12) {
            if let phase, let position {
                Text(label(phase))
                    .font(.headline)
                    .foregroundStyle(phase.kind == .work ? Color.green : Color.orange)
                Text(Format.clock(position.remaining))
                    .font(.system(size: 80, weight: .bold, design: .rounded))
                    .monospacedDigit()
                Text("\(workout.name(phase.slot.activityId)) · round \(phase.slot.round) of \(phase.slot.rounds)")
                    .font(.subheadline)
                if let hr = phase.slot.effort.metrics?["heart_rate"], phase.kind == .work {
                    Label("Heart rate \(hr.display)", systemImage: "heart.fill").foregroundStyle(.red)
                }
                if position.index + 1 < clock.phases.count {
                    Text("Then: " + label(clock.phases[position.index + 1]))
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                ProgressView(value: min(elapsed, clock.total), total: clock.total)
                Text("\(Format.clock(clock.total - elapsed)) left").font(.caption).foregroundStyle(.secondary)
            }
            Button("Stop", role: .destructive, action: onStop)
                .buttonStyle(.bordered)
                .controlSize(.large)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
    }

    private func label(_ phase: IntervalClock.Phase) -> String {
        switch (phase.kind, phase.side) {
        case (.rest, _): return "Rest"
        case (.work, .some(let side)): return "Work · \(side.rawValue) side"
        case (.work, .none): return "Work"
        }
    }
}

/// An exercise in the list: what it is, its targets, and how far you are.
private struct ExerciseRow: View {
    let exercise: Exercise
    let workout: Workout

    var body: some View {
        HStack {
            Image(systemName: exercise.done >= exercise.total ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(exercise.done >= exercise.total ? Color.green : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(workout.name(exercise.activityId)).foregroundStyle(.primary)
                if exercise.swapped {
                    Text("In place of \(workout.name(exercise.effort.activityId))")
                        .font(.caption).foregroundStyle(.indigo)
                }
                Text((exercise.total > 1 ? "\(exercise.total) × " : "") + Format.metrics(exercise.effort.metrics))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text("\(exercise.done)/\(exercise.total)").monospacedDigit().foregroundStyle(.secondary)
        }
    }
}
