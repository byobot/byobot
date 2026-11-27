// The paired laptop, what's waiting to send, your gym and equipment, and Apple Health.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var confirmForget = false
    @State private var confirmLeave = false

    var body: some View {
        Form {
            if model.demo {
                Section {
                    Button("Leave the demo", role: .destructive) { model.leaveDemo() }
                } header: {
                    Text("Demo")
                } footer: {
                    Text("You're looking at sample data. Leaving drops it and everything logged here, so you can pair.")
                }
            }
            if let server = model.server {
                Section("Laptop") {
                    LabeledContent("Name", value: server.name)
                    LabeledContent("Fingerprint", value: PairingLink.shortFingerprint(server.fingerprint))
                        .monospaced()
                    LabeledContent("Last sync", value: Format.ago(model.state.lastSyncAt))
                    Button(model.syncing ? "Syncing…" : "Sync now") { Task { await model.sync() } }
                        .disabled(model.syncing)
                }
            }
            Section {
                if let store = model.cloudStore {
                    LabeledContent("Logbook", value: String(store.suffix(8))).monospaced()
                    LabeledContent("Last sync", value: Format.ago(model.cloudSyncedAt))
                    if model.server == nil {
                        Button(model.syncing ? "Syncing…" : "Sync now") { Task { await model.sync() } }
                            .disabled(model.syncing)
                    }
                    Button("Stop Drive sync", role: .destructive) { confirmLeave = true }
                } else {
                    Text("Not on. On your Mac, run `byobot cloud join` and scan its code with the Camera app.")
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("Drive sync")
            } footer: {
                Text("Syncs with your Mac through your own Google Drive when the laptop is out of reach. Everything is encrypted on this phone first.")
            }
            if !model.book.conflicts.isEmpty {
                Section {
                    ForEach(model.book.conflicts) { conflict in
                        NavigationLink(ConflictView.title(conflict)) { ConflictView(conflict: conflict) }
                    }
                } header: {
                    Text("Conflicts")
                } footer: {
                    Text("Two devices or your coach changed these at once. Pick the version to keep.")
                }
            }
            Section {
                LabeledContent("Waiting to send", value: String(model.state.pending.count))
                ForEach(model.state.rejected) { rejected in
                    VStack(alignment: .leading) {
                        Text("Refused by your Mac").font(.subheadline)
                        Text(rejected.reason ?? "No reason given").font(.caption).foregroundStyle(.secondary)
                    }
                }
            } footer: {
                Text("What you log stays on this phone until it reaches your Mac.")
            }
            Section {
                GymMenu()
            } header: {
                Text("Gym")
            } footer: {
                Text("Loads compare only with sets at the same gym. Gyms you add on your laptop come with the plan.")
            }
            EquipmentSection()
            Section {
                if model.healthEnabled {
                    LabeledContent("Last sent", value: Format.ago(model.state.lastHealthSyncAt))
                    if model.healthNeedsAccess {
                        Button("Allow the new Apple Health data") { Task { await model.enableHealth() } }
                    }
                } else {
                    Button("Read from Apple Health") { Task { await model.enableHealth() } }
                        .disabled(!model.health.available)
                }
            } header: {
                Text("Apple Health")
            } footer: {
                Text("byobot reads body measurements, heart, activity, mobility, vitals, sleep and workouts, and sends them only to your Mac, directly or encrypted through your Google Drive if Drive sync is on. It never reads nutrition or cycle tracking. Where your laptop connects to a source like WHOOP directly, that source wins over its copy in Apple Health.")
            }
            if model.server != nil {
                Section {
                    Button("Forget this laptop", role: .destructive) { confirmForget = true }
                }
            }
            Section("This phone") {
                Text(model.state.deviceId).font(.caption.monospaced()).textSelection(.enabled)
            }
        }
        .navigationTitle("Settings")
        .confirmationDialog("Forget this laptop?", isPresented: $confirmForget, titleVisibility: .visible) {
            Button("Forget", role: .destructive) { Task { await model.forgetServer() } }
        } message: {
            Text("What you've logged stays on this phone and goes to the next laptop you pair. On the laptop, `byobot devices remove` revokes this phone.")
        }
        .confirmationDialog("Stop Drive sync?", isPresented: $confirmLeave, titleVisibility: .visible) {
            Button("Stop", role: .destructive) { model.leaveCloud() }
        } message: {
            Text("This phone signs out of Google and stops syncing through Drive. What you've logged stays here. On your Mac, `byobot cloud remove` takes it off the logbook.")
        }
    }
}

/// The equipment you have where you are, or by default, so swaps you can do come first.
private struct EquipmentSection: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let all = ActivityLibrary.equipment(in: model.book.library)
        Section {
            if all.isEmpty {
                Text("Your activities don't list any equipment yet.").foregroundStyle(.secondary)
            }
            ForEach(all, id: \.self) { item in
                Toggle(item.replacingOccurrences(of: "_", with: " ").capitalized, isOn: Binding(
                    get: { model.book.equipment?.contains(item) ?? true },
                    set: { on in
                        var have = model.book.equipment ?? Set(all)
                        if on { have.insert(item) } else { have.remove(item) }
                        model.setEquipment(have == Set(all) && model.book.location(model.book.currentLocationId) == nil ? nil : have)
                    }))
            }
        } header: {
            Text(model.book.location(model.book.currentLocationId).map { "Equipment at \($0.name)" } ?? "Your equipment")
        } footer: {
            Text("Swaps you have the equipment for come first. A location without its own list uses your default.")
        }
    }
}
