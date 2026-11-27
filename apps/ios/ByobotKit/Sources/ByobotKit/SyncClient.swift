// Talking to `byobot serve` over HTTPS on the local network, trusting only the pinned certificate.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import CryptoKit
import Foundation
import Security

public enum SyncClientError: Error, Equatable, LocalizedError {
    /// None of the laptop's addresses answered (it may be asleep, off the network, or not running `byobot serve`).
    case unreachable
    /// Something answered with a certificate other than the one this app pinned.
    case certificateMismatch(seen: String?)
    /// The laptop no longer knows this phone's token.
    case unauthorized
    case server(status: Int, error: String, message: String)
    case badResponse(String)

    public var errorDescription: String? {
        switch self {
        case .unreachable:
            return "Couldn't reach your laptop. Is `byobot serve` running, and is this phone on the same network?"
        case .certificateMismatch:
            return "Something answered that isn't your paired laptop, so nothing was sent."
        case .unauthorized:
            return "Your laptop doesn't know this phone anymore. Pair again."
        case .server(_, _, let message):
            return message
        case .badResponse(let what):
            return "Unexpected answer from your laptop: \(what)"
        }
    }
}

/// A client for one laptop. `fingerprint` is the pin; pass nil only to look at an unknown server
/// (`hello`) before the person confirms its fingerprint by eye.
public final class SyncClient: @unchecked Sendable {
    public let hosts: [String]
    public let port: Int
    public let fingerprint: String?
    public var token: String?
    private let session: URLSession
    private let pinner: Pinner
    private let lock = NSLock()
    private var preferredHost: String?

    public init(hosts: [String], port: Int, fingerprint: String?, token: String? = nil, timeout: TimeInterval = 8) {
        self.hosts = hosts
        self.port = port
        self.fingerprint = fingerprint?.lowercased()
        self.token = token
        self.pinner = Pinner(pin: self.fingerprint)
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout * 4
        config.waitsForConnectivity = false
        config.urlCache = nil
        self.session = URLSession(configuration: config, delegate: pinner, delegateQueue: nil)
    }

    public convenience init(server: PairedServer, token: String?) {
        self.init(hosts: server.hosts, port: server.port, fingerprint: server.fingerprint, token: token)
    }

    deinit {
        session.invalidateAndCancel()
    }

    /// The certificate fingerprint the last connection presented (for showing when pairing by hand).
    public var seenFingerprint: String? { pinner.seen }

    // MARK: Endpoints

    public func hello() async throws -> SyncHello {
        try await call("GET", "/v1/hello", body: Optional<Int>.none)
    }

    public func pair(code: String, deviceId: String, name: String) async throws -> PairResponse {
        try await call("POST", "/v1/pair", body: PairRequest(code: code, deviceId: deviceId, name: name))
    }

    public func status() async throws -> SyncStatus {
        try await call("GET", "/v1/status", body: Optional<Int>.none)
    }

    public func plan(from: String? = nil, to: String? = nil) async throws -> PlanWindow {
        var query: [String] = []
        if let from { query.append("from=\(from)") }
        if let to { query.append("to=\(to)") }
        return try await call("GET", "/v1/plan" + (query.isEmpty ? "" : "?" + query.joined(separator: "&")),
                              body: Optional<Int>.none)
    }

    public func push(events: [Event]) async throws -> EventsResponse {
        try await call("POST", "/v1/events", body: EventsRequest(events: events, kindVersions: KindVersion.all))
    }

    public func push(samples: [Sample], deleted: [Deletion] = []) async throws -> SamplesResponse {
        try await call("POST", "/v1/samples", body: SamplesRequest(samples: samples, deleted: deleted.isEmpty ? nil : deleted))
    }

    // MARK: Plumbing

    static func url(host: String, port: Int, path: String) -> URL? {
        let h = host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
        return URL(string: "https://\(h):\(port)\(path)")
    }

    private func orderedHosts() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        guard let preferred = preferredHost, hosts.contains(preferred) else { return hosts }
        return [preferred] + hosts.filter { $0 != preferred }
    }

    private func remember(_ host: String) {
        lock.lock()
        preferredHost = host
        lock.unlock()
    }

    private func call<Body: Encodable, Answer: Decodable>(_ method: String, _ path: String, body: Body?) async throws -> Answer {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.withoutEscapingSlashes]
        let payload = try body.map { try encoder.encode($0) }
        var lastError: Error = SyncClientError.unreachable
        for host in orderedHosts() {
            guard let url = Self.url(host: host, port: port, path: path) else { continue }
            var request = URLRequest(url: url)
            request.httpMethod = method
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            if let payload {
                request.httpBody = payload
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            }
            if let token {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }
            let answer: (Data, URLResponse)
            do {
                answer = try await session.data(for: request)
            } catch {
                if pinner.mismatched {
                    throw SyncClientError.certificateMismatch(seen: pinner.seen)
                }
                lastError = error
                continue  // try the next address
            }
            remember(host)
            return try Self.decode(data: answer.0, response: answer.1)
        }
        if lastError is SyncClientError { throw lastError }
        throw SyncClientError.unreachable
    }

    static func decode<Answer: Decodable>(data: Data, response: URLResponse) throws -> Answer {
        guard let http = response as? HTTPURLResponse else { throw SyncClientError.badResponse("not HTTP") }
        let decoder = JSONDecoder()
        if http.statusCode == 401 { throw SyncClientError.unauthorized }
        guard http.statusCode == 200 else {
            if let err = try? decoder.decode(SyncError.self, from: data) {
                throw SyncClientError.server(status: http.statusCode, error: err.error, message: err.message)
            }
            throw SyncClientError.server(status: http.statusCode, error: "http", message: "HTTP \(http.statusCode)")
        }
        do {
            return try decoder.decode(Answer.self, from: data)
        } catch {
            throw SyncClientError.badResponse(String(describing: error))
        }
    }
}

/// Accepts the server's certificate only when its SHA-256 fingerprint is the pinned one.
/// The certificate is self-signed, so the system's own trust evaluation is not used.
final class Pinner: NSObject, URLSessionDelegate, @unchecked Sendable {
    let pin: String?
    private let lock = NSLock()
    private var _seen: String?
    private var _mismatched = false

    init(pin: String?) {
        self.pin = pin
    }

    var seen: String? {
        lock.lock()
        defer { lock.unlock() }
        return _seen
    }

    var mismatched: Bool {
        lock.lock()
        defer { lock.unlock() }
        return _mismatched
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first
        else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        let der = SecCertificateCopyData(leaf) as Data
        let fingerprint = Self.fingerprint(der)
        let ok = pin == nil || pin == fingerprint
        lock.lock()
        _seen = fingerprint
        _mismatched = !ok
        lock.unlock()
        if ok {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    static func fingerprint(_ der: Data) -> String {
        SHA256.hash(data: der).hex
    }
}
