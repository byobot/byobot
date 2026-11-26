// Drive sync on this phone: the relay's state in a protected file beside the app's state, the Google sign-in and
// this phone's cloud keys in the keychain, and the sign-in itself in Safari's sheet.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import AuthenticationServices
import ByobotKit
import Foundation
import UIKit

enum CloudSetup {
    static let google = "byobot.google"
    static let keys = "byobot.cloud-keys"

    static var dir: URL { AppModel.stateFile().deletingLastPathComponent() }
    static var file: URL { dir.appendingPathComponent("cloud.json") }
    static var journal: URL { dir.appendingPathComponent("drive-ids.json") }

    static func load() -> CloudRelay? {
        (try? Data(contentsOf: file)).flatMap { try? JSONDecoder().decode(CloudRelay.self, from: $0) }
    }

    static func store(_ relay: CloudRelay) throws {
        try JSONEncoder().encode(relay).write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    static func tokens() -> GoogleTokens? {
        Keychain.data(service: google, account: "drive").flatMap { try? JSONDecoder().decode(GoogleTokens.self, from: $0) }
    }

    static func keep(_ tokens: GoogleTokens) {
        if let data = try? JSONEncoder().encode(tokens) { try? Keychain.set(data, service: google, account: "drive") }
    }

    /// This phone's cloud keys, made the first time they're needed.
    static func identity(for device: String) throws -> CloudIdentity {
        if let data = Keychain.data(service: keys, account: device), data.count == 64 {
            return try CloudIdentity(privateBox: data.prefix(32), privateSign: data.suffix(32))
        }
        let made = CloudIdentity()
        try Keychain.set(made.box.rawRepresentation + made.sign.rawRepresentation, service: keys, account: device)
        return made
    }

    static func route(_ relay: CloudRelay, tokens: GoogleTokens, identity: CloudIdentity) -> CloudRoute {
        let drive = Drive(tokens: tokens, saved: { keep($0) })
        let folder = DriveFolder(storeID: relay.storeID, drive: drive, journal: journal)
        return CloudRoute(relay: relay, folder: folder, identity: identity, save: { try store($0) })
    }

    static var carryFile: URL { dir.appendingPathComponent("health-carry.json") }

    /// Apple Health readings and deletions a relay held when this phone left Drive sync, sent by the next route.
    static func carry() -> SampleBatch {
        (try? Data(contentsOf: carryFile)).flatMap { try? JSONDecoder().decode(SampleBatch.self, from: $0) }
            ?? SampleBatch()
    }

    static func keepCarry(_ batch: SampleBatch) {
        if batch.samples.isEmpty && batch.deleted.isEmpty {
            try? FileManager.default.removeItem(at: carryFile)
        } else if let data = try? JSONEncoder().encode(batch) {
            try? data.write(to: carryFile, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        }
    }

    /// Forgets the relay and the sign-in. The keys stay, so joining again later is the same phone.
    static func forget() {
        if let left = load()?.leftovers {
            let had = carry()
            keepCarry(SampleBatch(samples: had.samples + left.samples, deleted: had.deleted + left.deleted))
        }
        try? FileManager.default.removeItem(at: file)
        try? FileManager.default.removeItem(at: journal)
        Keychain.delete(service: google, account: "drive")
    }
}

/// Google's sign-in page in Safari's sheet, for the drive.appdata scope alone.
@MainActor
final class GoogleSignInSheet: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var session: ASWebAuthenticationSession?  // held until Google answers

    nonisolated func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        MainActor.assumeIsolated {
            let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            let scene = scenes.first { $0.activationState == .foregroundActive } ?? scenes.first
            return scene?.keyWindow ?? scene.map { UIWindow(windowScene: $0) } ?? UIWindow()
        }
    }

    func signIn(clientID: String) async throws -> GoogleTokens {
        let request = Google.signIn(clientID: clientID)
        let callback: URL = try await withCheckedThrowingContinuation { done in
            let session = ASWebAuthenticationSession(url: request.url, callbackURLScheme: request.callbackScheme) { url, error in
                if let url {
                    done.resume(returning: url)
                } else {
                    done.resume(throwing: DriveError.upstream(
                        (error as? ASWebAuthenticationSessionError)?.code == .canceledLogin
                            ? "Sign-in cancelled." : "Google sign-in didn't finish. Try again."))
                }
            }
            session.presentationContextProvider = self
            self.session = session
            if !session.start() {
                done.resume(throwing: DriveError.upstream("Couldn't open Google's sign-in. Try again."))
            }
        }
        session = nil
        return try await Google.finish(request, callback: callback, http: URLSession.shared)
    }
}
