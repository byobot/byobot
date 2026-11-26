// Google Drive's hidden app folder as the phone's cloud folder, reached with the person's own iOS client.
// The same rules as python/src/byobot/cloud/drive.py: only the drive.appdata scope, each file written once with an
// id asked for up front, deleted outright, and one full listing followed by Drive's change feed.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

#if canImport(CryptoKit) && canImport(Compression)
import CryptoKit
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum DriveError: Error, Equatable, LocalizedError {
    /// Signed out of Drive, or never signed in: sign in again.
    case notConnected(String)
    case upstream(String)

    public var errorDescription: String? {
        switch self {
        case .notConnected(let why), .upstream(let why): return why
        }
    }
}

/// One HTTP exchange. URLSession in the app, a fake Drive in tests.
public protocol HTTPSending: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

extension URLSession: HTTPSending {
    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http)
    }
}

/// The sign-in byobot keeps: the iOS client it was made with and its tokens.
public struct GoogleTokens: Codable, Equatable, Sendable {
    public var clientID: String
    public var refreshToken: String
    public var accessToken: String
    public var expiresAt: Date

    public init(clientID: String, refreshToken: String, accessToken: String, expiresAt: Date) {
        self.clientID = clientID
        self.refreshToken = refreshToken
        self.accessToken = accessToken
        self.expiresAt = expiresAt
    }
}

public enum Google {
    public static let scope = "https://www.googleapis.com/auth/drive.appdata"
    static let authURL = "https://accounts.google.com/o/oauth2/v2/auth"
    static let tokenURL = "https://oauth2.googleapis.com/token"
    static let api = "https://www.googleapis.com/drive/v3"
    static let upload = "https://www.googleapis.com/upload/drive/v3/files"
    static let signedOut = "Google signed byobot out of Drive. Sign in again in Settings."

    /// A sign-in in the browser with PKCE, redirected back to the app by its iOS client's own scheme.
    public struct SignIn: Sendable {
        public let clientID: String
        public let verifier: String
        public let state: String
        public let url: URL

        /// The iOS client's reversed id, which Google sends the answer to.
        public var callbackScheme: String {
            clientID.split(separator: ".").reversed().joined(separator: ".")
        }

        var redirect: String { "\(callbackScheme):/oauth2redirect" }
    }

    public static func signIn(clientID: String) -> SignIn {
        let verifier = random(48), state = random(16)
        let challenge = base64url(Data(SHA256.hash(data: Data(verifier.utf8))))
        let scheme = clientID.split(separator: ".").reversed().joined(separator: ".")
        let query = form([("client_id", clientID), ("redirect_uri", "\(scheme):/oauth2redirect"), ("response_type", "code"),
                          ("scope", scope), ("code_challenge", challenge), ("code_challenge_method", "S256"),
                          ("state", state)])
        return SignIn(clientID: clientID, verifier: verifier, state: state, url: URL(string: "\(authURL)?\(query)")!)
    }

    /// Takes Google's redirect, trades its code for tokens, and checks the project is fit for daily use.
    public static func finish(_ signIn: SignIn, callback: URL, http: some HTTPSending,
                              now: Date = Date()) async throws -> GoogleTokens {
        let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? { items.first { $0.name == name }?.value }
        guard value("state") == signIn.state else {
            throw DriveError.upstream("The sign-in answer didn't match this request. Try again.")
        }
        guard let code = value("code") else {
            throw DriveError.upstream("Google didn't sign you in: \(value("error_description") ?? value("error") ?? "no answer")")
        }
        let answer = try await token(http, [("client_id", signIn.clientID), ("code", code),
                                            ("code_verifier", signIn.verifier), ("grant_type", "authorization_code"),
                                            ("redirect_uri", signIn.redirect)])
        guard let refresh = text(answer, "refresh_token"), let access = text(answer, "access_token") else {
            throw DriveError.upstream("Google gave no refresh token. Try again.")
        }
        if answer["refresh_token_expires_in"] != nil {
            throw DriveError.upstream("Your Google Cloud project is in Testing, which signs byobot out every seven "
                                      + "days. Publish it so it's In production, then sign in again.")
        }
        let granted = Set((text(answer, "scope") ?? scope).split(separator: " ").map(String.init))
        guard granted == [scope] else {
            throw DriveError.upstream("Google granted more than byobot asks for. Give the consent screen only the "
                                      + "drive.appdata scope, then sign in again.")
        }
        return GoogleTokens(clientID: signIn.clientID, refreshToken: refresh, accessToken: access,
                            expiresAt: now.addingTimeInterval(seconds(answer["expires_in"])))
    }

    static func token(_ http: some HTTPSending, _ fields: [(String, String)]) async throws -> [String: JSONValue] {
        var request = URLRequest(url: URL(string: tokenURL)!)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(form(fields).utf8)
        let result: (Data, HTTPURLResponse)
        do {
            result = try await http.send(request)
        } catch {
            throw DriveError.upstream("Can't reach Google: \(error.localizedDescription)")
        }
        let (data, response) = result
        var body: [String: JSONValue] = [:]
        if case .object(let o)? = try? JSONValue.parse(data) { body = o }
        guard response.statusCode == 200 else {
            if text(body, "error") == "invalid_grant" { throw DriveError.notConnected(signedOut) }
            throw DriveError.upstream("Google refused the sign-in (\(response.statusCode)).")
        }
        return body
    }

    // MARK: helpers

    static let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    static func form(_ fields: [(String, String)]) -> String {
        fields.map { "\($0.0)=\($0.1.addingPercentEncoding(withAllowedCharacters: unreserved) ?? "")" }
            .joined(separator: "&")
    }

    static func random(_ bytes: Int) -> String {
        base64url(Data((0..<bytes).map { _ in UInt8.random(in: 0...255) }))
    }

    static func base64url(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func text(_ object: [String: JSONValue], _ key: String) -> String? {
        if case .string(let s)? = object[key] { return s }
        return nil
    }

    static func seconds(_ value: JSONValue?) -> TimeInterval {
        if case .number(let n)? = value { return n }
        return 3600
    }
}

/// Drive API v3 calls with the saved sign-in: refreshes the access token, and backs off and retries when Drive is
/// busy or the network drops.
public actor Drive {
    static let tries = 6
    static let retry: Set<Int> = [429, 500, 502, 503, 504]
    static let rate: Set<String> = ["rateLimitExceeded", "userRateLimitExceeded"]

    enum Answer: Error { case notFound, conflict }

    let http: any HTTPSending
    public private(set) var tokens: GoogleTokens
    let saved: @Sendable (GoogleTokens) async -> Void
    let sleep: @Sendable (Double) async -> Void
    let now: @Sendable () -> Date

    public init(tokens: GoogleTokens, http: any HTTPSending = URLSession.shared,
                saved: @escaping @Sendable (GoogleTokens) async -> Void = { _ in },
                sleep: @escaping @Sendable (Double) async -> Void = { try? await Task.sleep(nanoseconds: UInt64($0 * 1e9)) },
                now: @escaping @Sendable () -> Date = { Date() }) {
        self.tokens = tokens
        self.http = http
        self.saved = saved
        self.sleep = sleep
        self.now = now
    }

    func refresh() async throws {
        let answer = try await Google.token(http, [("client_id", tokens.clientID), ("grant_type", "refresh_token"),
                                                   ("refresh_token", tokens.refreshToken)])
        guard let access = Google.text(answer, "access_token") else {
            throw DriveError.upstream("Google gave no access token.")
        }
        tokens.accessToken = access
        tokens.refreshToken = Google.text(answer, "refresh_token") ?? tokens.refreshToken
        tokens.expiresAt = now().addingTimeInterval(Google.seconds(answer["expires_in"]))
        await saved(tokens)
    }

    /// One call's body. Throws Answer.notFound on 404 and Answer.conflict on 409.
    func call(_ method: String, _ url: String, _ query: [(String, String)] = [], body: Data? = nil,
              headers: [String: String] = [:]) async throws -> Data {
        let full = URL(string: query.isEmpty ? url : "\(url)?\(Google.form(query))")!
        var refreshed = false
        for attempt in 0..<Self.tries {
            if tokens.expiresAt.timeIntervalSince(now()) < 60 { try await refresh() }
            var request = URLRequest(url: full)
            request.httpMethod = method
            request.httpBody = body
            for (k, v) in headers { request.setValue(v, forHTTPHeaderField: k) }
            request.setValue("Bearer \(tokens.accessToken)", forHTTPHeaderField: "Authorization")
            do {
                let (data, response) = try await http.send(request)
                let code = response.statusCode
                if (200..<300).contains(code) { return data }
                let (message, reason) = Self.why(data)
                if code == 401, !refreshed {
                    try await refresh()
                    refreshed = true
                    continue
                }
                if code == 404 { throw Answer.notFound }
                if code == 409 { throw Answer.conflict }
                if code == 403, reason == "storageQuotaExceeded" {
                    throw DriveError.upstream("Your Google Drive is full, so byobot can't upload. Free some space.")
                }
                if !(Self.retry.contains(code) || (code == 403 && Self.rate.contains(reason))) || attempt == Self.tries - 1 {
                    throw DriveError.upstream("Google Drive answered \(code)\(message.isEmpty ? "" : ": \(message)")")
                }
            } catch let error as URLError {
                if attempt == Self.tries - 1 {
                    throw DriveError.upstream("Can't reach Google Drive: \(error.localizedDescription)")
                }
            }
            await sleep(min(pow(2, Double(attempt)), 32) + Double.random(in: 0..<1))
        }
        throw DriveError.upstream("Google Drive didn't answer.")
    }

    func json(_ method: String, _ path: String, _ query: [(String, String)] = []) async throws -> [String: JSONValue] {
        let data = try await call(method, Google.api + path, query)
        if data.isEmpty { return [:] }
        guard case .object(let o) = try JSONValue.parse(data) else { throw DriveError.upstream("Unexpected answer from Google Drive.") }
        return o
    }

    static func why(_ data: Data) -> (String, String) {
        guard case .object(let body)? = try? JSONValue.parse(data.prefix(8000)),
              case .object(let error)? = body["error"] else { return ("", "") }
        var reason = ""
        if case .array(let errors)? = error["errors"], case .object(let first)? = errors.first {
            reason = Google.text(first, "reason") ?? ""
        }
        return (String((Google.text(error, "message") ?? "").prefix(200)), reason)
    }
}

/// One store's cloud folder: files named `<store_id>/<path>` in the hidden app folder, each marked with the store in
/// its appProperties. Ids asked for before an upload are kept in a small journal, so an upload cut short is retried
/// with the same id and never lands twice.
public actor DriveFolder: CloudFolder {
    static let store = "byobot_store"
    static let fields = "id,name,sha256Checksum,appProperties,trashed"

    public let storeID: String
    let drive: Drive
    let journal: URL
    var index: [String: [(id: String, sum: String)]]?  // path -> files, oldest first
    var token: String?
    var loadedAt = Date.distantPast
    /// A full listing this often, so a change feed that lags or misses a file never hides it for long.
    static let relist: TimeInterval = 600

    public init(storeID: String, drive: Drive, journal: URL) {
        self.storeID = storeID
        self.drive = drive
        self.journal = journal
    }

    // MARK: the journal: path -> the id asked for before its upload

    func ids() -> [String: String] {
        guard let data = try? Data(contentsOf: journal),
              let ids = try? JSONDecoder().decode([String: String].self, from: data) else { return [:] }
        return ids
    }

    func keep(_ path: String, _ id: String?) throws {
        var all = ids()
        if let id { all[path] = id } else if all.removeValue(forKey: path) == nil { return }
        try FileManager.default.createDirectory(at: journal.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(all).write(to: journal, options: .atomic)
    }

    // MARK: the index, from one full listing, then the change feed

    func path(_ file: [String: JSONValue]) -> String? {
        guard file["trashed"] != .bool(true), case .object(let props)? = file["appProperties"],
              props[Self.store] == .string(storeID), let name = Google.text(file, "name"),
              name.hasPrefix(storeID + "/") else { return nil }
        return String(name.dropFirst(storeID.count + 1))
    }

    func add(_ file: [String: JSONValue]) {
        guard let path = path(file), let id = Google.text(file, "id") else { return }
        index?[path, default: []].append((id, Google.text(file, "sha256Checksum") ?? "id:\(id)"))
    }

    func dropID(_ id: String) {
        guard let current = index else { return }
        for (path, entries) in current where entries.contains(where: { $0.id == id }) {
            let kept = entries.filter { $0.id != id }
            index?[path] = kept.isEmpty ? nil : kept
        }
    }

    func load() async throws {
        token = Google.text(try await drive.json("GET", "/changes/startPageToken"), "startPageToken")
        index = [:]
        loadedAt = Date()
        var query = [("spaces", "appDataFolder"), ("pageSize", "1000"), ("orderBy", "createdTime"),
                     ("q", "appProperties has { key='\(Self.store)' and value='\(storeID)' }"),
                     ("fields", "nextPageToken,files(\(Self.fields))")]
        while true {
            let page = try await drive.json("GET", "/files", query)
            if case .array(let files)? = page["files"] {
                for case .object(let f) in files { add(f) }
            }
            guard let next = Google.text(page, "nextPageToken") else { return }
            query.removeAll { $0.0 == "pageToken" }
            query.append(("pageToken", next))
        }
    }

    /// Reads Drive's change feed; true if any of this store's files came or went since the last look.
    public func changed() async throws -> Bool {
        guard index != nil, let start = token, Date().timeIntervalSince(loadedAt) < Self.relist else {
            try await load()
            return true
        }
        var seen = false, pageToken = start
        while true {
            let page = try await drive.json("GET", "/changes", [
                ("pageToken", pageToken), ("spaces", "appDataFolder"), ("pageSize", "1000"),
                ("fields", "nextPageToken,newStartPageToken,changes(fileId,removed,file(\(Self.fields)))")])
            if case .array(let changes)? = page["changes"] {
                for case .object(let c) in changes {
                    guard let id = Google.text(c, "fileId") else { continue }
                    var ours = (index ?? [:]).values.contains { $0.contains { $0.id == id } }
                    dropID(id)
                    if c["removed"] != .bool(true), case .object(let file)? = c["file"] {
                        ours = ours || path(file) != nil
                        add(file)
                    }
                    seen = seen || ours
                }
            }
            if let fresh = Google.text(page, "newStartPageToken") {
                token = fresh
                return seen
            }
            guard let next = Google.text(page, "nextPageToken") else { return seen }
            pageToken = next
        }
    }

    // MARK: CloudFolder

    public func list() async throws -> [String: String] {
        _ = try await changed()
        return (index ?? [:]).compactMapValues { $0.first?.sum }
    }

    public func read(_ path: String) async throws -> Data? {
        let key = try Self.safe(path)
        if index == nil { try await load() }
        for entry in index?[key] ?? [] {
            do {
                return try await drive.call("GET", "\(Google.api)/files/\(entry.id)", [("alt", "media")])
            } catch Drive.Answer.notFound {
                dropID(entry.id)
            }
        }
        return nil
    }

    public func create(_ path: String, _ data: Data) async throws {
        let key = try Self.safe(path)
        _ = try await changed()
        if index?[key] != nil {
            try keep(path, nil)
            throw CloudExists(path)
        }
        let sum = Self.checksum(data)
        var fileID = ids()[path]
        for _ in 0..<2 {
            if fileID == nil {
                let made = try await drive.json("GET", "/files/generateIds",
                                                [("count", "1"), ("space", "appDataFolder"), ("type", "files")])
                guard case .array(let fresh)? = made["ids"], case .string(let id)? = fresh.first else {
                    throw DriveError.upstream("Google Drive gave no file id.")
                }
                fileID = id
                try keep(path, id)
            }
            let id = fileID!
            let made: [String: JSONValue]
            do {
                made = try await upload(id, path, data)
            } catch Drive.Answer.conflict {  // an earlier try with this id landed, or the id was used and deleted since
                var found: [String: JSONValue]?
                do {
                    found = try await drive.json("GET", "/files/\(id)", [("fields", Self.fields)])
                } catch Drive.Answer.notFound {
                    found = nil
                }
                try keep(path, nil)
                guard let found, self.path(found) == path else {
                    fileID = nil
                    continue
                }
                add(found)
                if let other = Google.text(found, "sha256Checksum"), other != sum { throw CloudExists(path) }
                return
            }
            add(made)
            try keep(path, nil)
            if let other = Google.text(made, "sha256Checksum"), other != sum {
                try await delete(path)
                throw DriveError.upstream("\(path) reached Google Drive damaged and was deleted. It goes again next sync.")
            }
            return
        }
        throw DriveError.upstream("Google Drive wouldn't take \(path). Try again.")
    }

    func upload(_ id: String, _ path: String, _ data: Data) async throws -> [String: JSONValue] {
        let meta: JSONValue = .object(["id": .string(id), "name": .string("\(storeID)/\(path)"),
                                       "parents": .array([.string("appDataFolder")]),
                                       "appProperties": .object([Self.store: .string(storeID)])])
        let boundary = Google.random(16).replacingOccurrences(of: "-", with: "x").replacingOccurrences(of: "_", with: "y")
        var body = Data("--\(boundary)\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".utf8)
        body += try JSONEncoder().encode(meta)
        body += Data("\r\n--\(boundary)\r\nContent-Type: application/octet-stream\r\n\r\n".utf8)
        body += data
        body += Data("\r\n--\(boundary)--\r\n".utf8)
        let answer = try await drive.call("POST", Google.upload, [("uploadType", "multipart"), ("fields", Self.fields)],
                                          body: body, headers: ["Content-Type": "multipart/related; boundary=\(boundary)"])
        guard case .object(let o) = try JSONValue.parse(answer) else { throw DriveError.upstream("Unexpected answer from Google Drive.") }
        return o
    }

    public func delete(_ path: String) async throws {
        let key = try Self.safe(path)
        if index == nil { try await load() }
        let gone = index?.removeValue(forKey: key) ?? []
        for entry in gone {
            do {
                _ = try await drive.call("DELETE", "\(Google.api)/files/\(entry.id)")
            } catch Drive.Answer.notFound {}
        }
        try keep(path, nil)
    }

    static func safe(_ path: String) throws -> String {
        let parts = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard !parts.isEmpty, parts.allSatisfy(Cloud.isID) else { throw CloudError("not a cloud path: \(path)") }
        return path
    }

    static func checksum(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
#endif
