// Copyright The byobot Authors. Licensed under Apache-2.0.

import SwiftUI
import UIKit

struct ContentView: View {
    @ObservedObject private var log = ProbeLog.shared

    var body: some View {
        NavigationStack {
            List {
                Section("Device token") {
                    if let token = log.token {
                        Text(token).font(.caption.monospaced()).textSelection(.enabled)
                        Button("Copy") { UIPasteboard.general.string = token }
                    } else {
                        Text(log.tokenError ?? "Registering…").foregroundStyle(.secondary)
                    }
                }
                Section {
                    Picker("Condition", selection: Binding(get: { log.condition }, set: { log.setCondition($0) })) {
                        ForEach(ProbeLog.conditions, id: \.self) { Text($0.replacingOccurrences(of: "_", with: " ")) }
                    }
                    .pickerStyle(.segmented)
                } header: {
                    Text("Condition")
                } footer: {
                    Text("Set this before you turn on Low Power Mode or swipe the app away. Locked or unlocked is recorded on its own.")
                }
                Section("\(log.pushes.count) pushes received") {
                    ForEach(log.pushes.prefix(100)) { push in
                        HStack {
                            Text(push.pushId.prefix(8)).font(.caption.monospaced())
                            Spacer()
                            Text(push.locked ? "\(push.state), locked" : push.state).foregroundStyle(.secondary)
                            Text(push.latency.map { String(format: "%.0fs", $0) } ?? "–").monospacedDigit()
                        }
                    }
                }
            }
            .navigationTitle("push probe")
            .toolbar {
                ShareLink(item: log.url) { Label("Export log", systemImage: "square.and.arrow.up") }
            }
        }
    }
}
