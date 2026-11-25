// Where the app opens: the session you're in, or the next one up with one tap to start.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct TodayView: View {
    @EnvironmentObject var model: AppModel
    @State private var path: [String] = []

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let active = model.book.activeSession {
                    Section("In progress") { current(active) }
                } else if let next = model.book.upNext(today: LocalDate.today()) {
                    Section(Format.day(next.scheduledOn)) { upNext(next) }
                } else {
                    Section {
                        Text(model.plan == nil ? "Pull down to get your plan from your laptop."
                             : "Nothing left in your plan. Ask your coach for the next one, then pull down to sync.")
                            .foregroundStyle(.secondary)
                    }
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
            .navigationTitle("Today")
            .navigationDestination(for: String.self) { WorkoutView(plannedId: $0) }
        }
    }

    @ViewBuilder
    private func current(_ active: PerformedSession) -> some View {
        if let id = active.plannedSessionId, let planned = model.plan?.plannedSessions.first(where: { $0.id == id }) {
            let workout = model.book.workout(planned)
            Button {
                path.append(planned.id)
            } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text(planned.name).font(.title2.bold()).foregroundStyle(.primary)
                    Text("\(workout.done) of \(workout.total) sets done").foregroundStyle(.secondary)
                    if let next = workout.next {
                        Text("Next: \(workout.name(next.activityId))").foregroundStyle(.secondary)
                    }
                    Text("Continue").font(.headline).frame(maxWidth: .infinity).padding(.vertical, 10)
                        .background(Color.accentColor, in: RoundedRectangle(cornerRadius: 10))
                        .foregroundStyle(.white)
                }
            }
        } else {
            NavigationLink {
                SessionView(plannedId: active.plannedSessionId)
            } label: {
                Label("Continue \(active.name ?? "session")", systemImage: "play.circle.fill")
            }
        }
    }

    @ViewBuilder
    private func upNext(_ planned: PlannedSession) -> some View {
        let workout = model.book.workout(planned)
        VStack(alignment: .leading, spacing: 8) {
            Text(planned.name).font(.title2.bold())
            ForEach(workout.exercises.prefix(5)) { exercise in
                Text(workout.name(exercise.activityId)
                     + (exercise.total > 1 ? ", \(exercise.total) sets" : ""))
                    .foregroundStyle(.secondary)
            }
            if workout.exercises.count > 5 {
                Text("and \(workout.exercises.count - 5) more").foregroundStyle(.secondary)
            }
            Button {
                model.start(planned)
                path.append(planned.id)
            } label: {
                Text("Start").font(.headline).frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            Button("See it first") { path.append(planned.id) }
                .buttonStyle(.borderless)
                .frame(maxWidth: .infinity)
        }
        .padding(.vertical, 4)
    }
}
