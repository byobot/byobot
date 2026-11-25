// The paired laptop, what's waiting to send, your gym and equipment, and Apple Health.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var confirmForget = false

    var body: some View {
        Form {
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
                LabeledContent("Waiting to send", value: String(model.state.pending.count))
                ForEach(model.state.rejected) { rejected in
                    VStack(alignment: .leading) {
                        Text("Refused by your laptop").font(.subheadline)
                        Text(describe(rejected.errors)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            } footer: {
                Text("What you log stays on this phone until it reaches your laptop.")
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
                Text("byobot reads body measurements, heart, activity, mobility, vitals, sleep and workouts, and sends them only to your laptop. It never reads nutrition or cycle tracking. Where your laptop connects to a source like WHOOP directly, that source wins over its copy in Apple Health.")
            }
            Section {
                Button("Forget this laptop", role: .destructive) { confirmForget = true }
            }
            Section("This phone") {
                Text(model.state.deviceId).font(.caption.monospaced()).textSelection(.enabled)
            }
        }
        .navigationTitle("Settings")
        .confirmationDialog("Forget this laptop?", isPresented: $confirmForget, titleVisibility: .visible) {
            Button("Forget", role: .destructive) { model.forgetServer() }
        } message: {
            Text("What you've logged stays on this phone and goes to the next laptop you pair. On the laptop, `byobot unpair` revokes this phone.")
        }
    }

    private func describe(_ errors: [JSONValue]) -> String {
        let messages = errors.compactMap { error -> String? in
            if case .object(let o) = error, case .string(let m)? = o["message"] { return m }
            if case .string(let m) = error { return m }
            return nil
        }
        return messages.isEmpty ? "No reason given" : messages.joined(separator: "; ")
    }
}

/// The equipment you have, so swaps you can do come first. Nothing picked means everything counts.
private struct EquipmentSection: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let all = ActivityLibrary.equipment(in: model.book.library)
        Section {
            if all.isEmpty {
                Text("Your activities don't list any equipment yet.").foregroundStyle(.secondary)
            }
            ForEach(all, id: \.self) { item in
                Toggle(item.replacingOccurrences(of: "-", with: " ").capitalized, isOn: Binding(
                    get: { model.book.equipment?.contains(item) ?? true },
                    set: { on in
                        var have = model.book.equipment ?? Set(all)
                        if on { have.insert(item) } else { have.remove(item) }
                        model.setEquipment(have == Set(all) ? nil : have)
                    }))
            }
        } header: {
            Text("Your equipment")
        } footer: {
            Text("Swaps you have the equipment for come first.")
        }
    }
}
