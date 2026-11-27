// Four tabs: today's session first, then the week, history and settings.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct RootView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Group {
            if !model.connected {
                NavigationStack { PairView() }
            } else {
                TabView {
                    TodayView()
                        .tabItem { Label("Today", systemImage: "figure.strengthtraining.traditional") }
                    NavigationStack { PlanView() }
                        .tabItem { Label("Week", systemImage: "calendar") }
                    NavigationStack { HistoryView() }
                        .tabItem { Label("History", systemImage: "clock.arrow.circlepath") }
                    NavigationStack { SettingsView() }
                        .tabItem { Label("Settings", systemImage: "gearshape") }
                }
            }
        }
        .sheet(item: Binding(get: { model.pendingLink.map(LinkBox.init) }, set: { if $0 == nil { model.pendingLink = nil } })) {
            ConfirmPairView(link: $0.link).environmentObject(model)
        }
        .sheet(item: $model.pendingCloud) {
            ConfirmCloudView(pending: $0).environmentObject(model)
        }
        .alert("byobot", isPresented: Binding(get: { model.message != nil }, set: { if !$0 { model.message = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.message ?? "")
        }
    }
}

private struct LinkBox: Identifiable {
    var link: PairingLink
    var id: String { link.code + link.fingerprint }
}

/// Shown when a pairing QR opens the app: who it is, and the fingerprint to compare with the laptop's screen.
struct ConfirmPairView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let link: PairingLink
    @State private var working = false
    @State private var error: String?
    @State private var newName: String?
    @State private var confirming = false

    var body: some View {
        NavigationStack {
            Form {
                if let old = model.server, link.replaces(old) {
                    Section {
                        LabeledContent("Paired now", value: old.name)
                        LabeledContent("Fingerprint", value: PairingLink.shortFingerprint(old.fingerprint)).monospaced()
                    } footer: {
                        Text("Pairing with this laptop replaces \(old.name). New sets and Health data go to it instead.")
                    }
                }
                Section {
                    if let newName { LabeledContent("Laptop", value: newName) }
                    LabeledContent("Address", value: link.hosts.joined(separator: ", "))
                    LabeledContent("Port", value: String(link.port))
                    LabeledContent("Fingerprint", value: PairingLink.shortFingerprint(link.fingerprint))
                        .monospaced()
                } footer: {
                    Text("The fingerprint should match the one `byobot serve` printed on your laptop.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button(buttonTitle) {
                        if replacing { confirming = true } else { pair() }
                    }
                    .disabled(working)
                }
            }
            .confirmationDialog("Replace \(model.server?.name ?? "this laptop")?", isPresented: $confirming,
                                titleVisibility: .visible) {
                Button("Replace with \(newName ?? link.hosts.first ?? "the new laptop")", role: .destructive) { pair() }
            } message: {
                Text("Only do this if the fingerprint matches the one on the new laptop's screen.")
            }
            .task {
                guard let host = link.hosts.first, let found = try? await model.probe(host: host, port: link.port),
                      found.fingerprint == link.fingerprint else { return }
                newName = found.hello.server.name
            }
            .navigationTitle("Pair")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}

extension ConfirmPairView {
    private var replacing: Bool { link.replaces(model.server) }

    private var buttonTitle: String {
        if working { return "Pairing…" }
        return replacing ? "Replace \(model.server?.name ?? "laptop")" : "Pair with this laptop"
    }

    private func pair() {
        Task {
            working = true
            defer { working = false }
            do {
                try await model.pair(hosts: link.hosts, port: link.port, code: link.code, fingerprint: link.fingerprint)
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

/// Shown when a Drive sync code opens the app: what joining does, then Google's sign-in.
struct ConfirmCloudView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let pending: CloudJoinLink
    @State private var working = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("This phone will sync with your Mac through your own Google Drive, wherever you are. "
                         + "byobot uses a hidden folder only it can see, and encrypts everything before it leaves "
                         + "the phone.")
                } footer: {
                    Text("Sign in with the Google account you set up Drive sync with on your Mac.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button(working ? "Joining…" : "Sign in with Google and join") {
                        Task {
                            working = true
                            defer { working = false }
                            do {
                                try await model.joinCloud(pending)
                                dismiss()
                            } catch {
                                self.error = error.localizedDescription
                            }
                        }
                    }
                    .disabled(working)
                }
            }
            .navigationTitle("Drive sync")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }
}
