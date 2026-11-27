// Where the app keeps its state on the phone.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// The app's state file. One that exists but doesn't decode, or is from a newer app, is moved aside, never
/// overwritten, so a build that reads it can still bring back unsynced sets, the pairing and the device id.
public struct StateFile: Sendable {
    public enum Outcome: Equatable, Sendable {
        case loaded
        case new
        /// The old file is kept here and the app starts fresh.
        case setAside(URL)
        /// The file is there but couldn't be read or moved, so nothing is saved over it.
        case cantRead
    }

    public struct Locked: LocalizedError {
        public init() {}
        public var errorDescription: String? {
            "Couldn't read what this phone keeps, so nothing is saved until you open the app again."
        }
    }

    public let url: URL
    public private(set) var locked = false

    public init(url: URL) {
        self.url = url
    }

    public mutating func load() -> (state: AppState, outcome: Outcome) {
        let fm = FileManager.default
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            guard fm.fileExists(atPath: url.path) else { return (AppState(), .new) }
            locked = true  // for example still locked before the first unlock: not unreadable, just not yet
            return (AppState(), .cantRead)
        }
        if let state = try? JSONDecoder().decode(AppState.self, from: data), state.version <= AppState.currentVersion {
            return (state, .loaded)
        }
        let aside = Self.unused(url)
        do {
            try fm.moveItem(at: url, to: aside)
        } catch {
            locked = true
            return (AppState(), .cantRead)
        }
        return (AppState(), .setAside(aside))
    }

    public func save(_ state: AppState, options: Data.WritingOptions = .atomic) throws {
        guard !locked else { throw Locked() }
        try JSONEncoder().encode(state).write(to: url, options: options)
    }

    /// `state.json.unreadable`, else the first of `state.json.unreadable-2`, `-3`, ... not taken yet.
    static func unused(_ url: URL) -> URL {
        let dir = url.deletingLastPathComponent(), name = url.lastPathComponent + ".unreadable"
        var candidate = dir.appendingPathComponent(name)
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = dir.appendingPathComponent("\(name)-\(n)")
            n += 1
        }
        return candidate
    }
}
