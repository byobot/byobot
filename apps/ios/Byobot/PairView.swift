// Pairing with `byobot serve`: scan its QR with the Camera app (it opens byobot), or type
// the address and code and compare the fingerprint by eye.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

struct PairView: View {
    @EnvironmentObject var model: AppModel
    @State private var host = ""
    @State private var port = "8765"
    @State private var code = ""
    @State private var link = ""
    @State private var found: (hello: SyncHello, fingerprint: String)?
    @State private var working = false
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                Text("On your laptop, run `byobot serve`. Scan the QR code it shows with the Camera app, or enter its details below.")
            }
            Section("Pairing link") {
                TextField("byobot://pair?…", text: $link)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("Use link") { model.open(URL(string: link) ?? URL(string: "byobot://invalid")!) }
                    .disabled(link.isEmpty)
            }
            Section {
                TextField("Address, e.g. my-laptop.local", text: $host)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                TextField("Port", text: $port).keyboardType(.numberPad)
                TextField("Six-digit code", text: $code).keyboardType(.numberPad)
                Button("Find laptop") { Task { await probe() } }
                    .disabled(host.isEmpty || Int(port) == nil || working)
            } header: {
                Text("By hand")
            }
            if let found {
                Section {
                    LabeledContent("Laptop", value: found.hello.server.name)
                    LabeledContent("Fingerprint", value: PairingLink.shortFingerprint(found.fingerprint))
                        .monospaced()
                    Button("It matches. Pair") { Task { await pair(found.fingerprint) } }
                        .disabled(code.count != 6 || working)
                } footer: {
                    Text("Pair only if this fingerprint matches the one on your laptop's screen.")
                }
            }
            if let error {
                Section { Text(error).foregroundStyle(.red) }
            }
            Section {
                Button("Try it with sample data") { model.startDemo() }
            } footer: {
                Text("A sample plan and history to look around with. Nothing leaves this phone.")
            }
        }
        .navigationTitle("Pair with your laptop")
    }

    private func probe() async {
        working = true
        defer { working = false }
        error = nil
        do {
            found = try await model.probe(host: host.trimmingCharacters(in: .whitespaces), port: Int(port) ?? 8765)
        } catch {
            found = nil
            self.error = error.localizedDescription
        }
    }

    private func pair(_ fingerprint: String) async {
        working = true
        defer { working = false }
        do {
            try await model.pair(hosts: [host.trimmingCharacters(in: .whitespaces)], port: Int(port) ?? 8765,
                                 code: code, fingerprint: fingerprint)
        } catch {
            self.error = error.localizedDescription
        }
    }
}
