// Logs every silent push it receives, to measure how reliably iOS wakes an app.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import SwiftUI
import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        ProbeLog.shared.launched(byPush: launchOptions?[.remoteNotification] != nil, state: application.applicationState)
        application.registerForRemoteNotifications()
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        ProbeLog.shared.setToken(deviceToken.map { String(format: "%02x", $0) }.joined())
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        ProbeLog.shared.setToken(nil, error: error.localizedDescription)
    }

    func application(
        _ application: UIApplication,
        didReceiveRemoteNotification userInfo: [AnyHashable: Any],
        fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        ProbeLog.shared.recordPush(userInfo, application: application)
        completionHandler(.newData)
    }
}

@main
struct PushProbeApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .onChange(of: phase) { _, now in
            switch now {
            case .active: ProbeLog.shared.phase("foreground")
            case .background: ProbeLog.shared.phase("background")
            default: break
            }
        }
    }
}
