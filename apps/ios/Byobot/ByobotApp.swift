// The byobot iPhone app: your week's plan from your laptop, logged set by set, offline.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import ByobotKit
import SwiftUI

@main
struct ByobotApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                // A pairing QR from `byobot serve`, scanned with the Camera app, opens here.
                .onOpenURL { model.open($0) }
        }
        .onChange(of: phase) { _, now in
            // Sync whenever the app comes to the front. Nothing syncs in the background.
            if now == .active { Task { await model.sync() } }
        }
    }
}
