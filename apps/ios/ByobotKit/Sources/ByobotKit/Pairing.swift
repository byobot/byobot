// The `byobot://pair` link that `byobot serve` shows as a QR code.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Where the laptop is and how to trust it: hosts to try, a port, a one-time code, and the
/// SHA-256 fingerprint of its certificate, which the app pins from then on.
public struct PairingLink: Equatable, Sendable {
    public var hosts: [String]
    public var port: Int
    public var code: String
    /// Lowercase hex SHA-256 of the server certificate's DER bytes.
    public var fingerprint: String

    public init(hosts: [String], port: Int, code: String, fingerprint: String) {
        self.hosts = hosts
        self.port = port
        self.code = code
        self.fingerprint = fingerprint
    }

    public enum Problem: Error, Equatable {
        case notAPairingLink
        case unsupportedProtocol(Int)
        case missing(String)
        case invalid(String)
    }

    /// Parse `byobot://pair?v=1&host=...&host=...&port=8765&code=123456&fp=<base64url>`.
    public init(link: String) throws {
        guard let parts = URLComponents(string: link), parts.scheme == "byobot", parts.host == "pair" else {
            throw Problem.notAPairingLink
        }
        let items = parts.queryItems ?? []
        func first(_ name: String) throws -> String {
            guard let value = items.first(where: { $0.name == name })?.value, !value.isEmpty else {
                throw Problem.missing(name)
            }
            return value
        }
        guard let version = Int(try first("v")) else { throw Problem.invalid("v") }
        guard version == ContractVersion.sync else { throw Problem.unsupportedProtocol(version) }
        let hosts = items.filter { $0.name == "host" }.compactMap(\.value).filter { !$0.isEmpty }
        guard !hosts.isEmpty else { throw Problem.missing("host") }
        guard let port = Int(try first("port")), (1...65535).contains(port) else { throw Problem.invalid("port") }
        let code = try first("code")
        guard code.count == 6, code.allSatisfy(\.isASCII), code.allSatisfy(\.isNumber) else {
            throw Problem.invalid("code")
        }
        guard let fp = Self.hex(fromBase64URL: try first("fp")), fp.count == 64 else { throw Problem.invalid("fp") }
        self.init(hosts: hosts, port: port, code: code, fingerprint: fp)
    }

    static func hex(fromBase64URL text: String) -> String? {
        var b64 = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        while b64.count % 4 != 0 { b64 += "=" }
        guard let data = Data(base64Encoded: b64) else { return nil }
        return data.map { String(format: "%02x", $0) }.joined()
    }

    /// The link with only its hosts on this network, or nil when it has none. A link can come from
    /// anywhere, and a laptop elsewhere is reached through `byobot remote`, never by pairing.
    public var localOnly: PairingLink? {
        let local = hosts.filter(Self.isLocal)
        return local.isEmpty ? nil : PairingLink(hosts: local, port: port, code: code, fingerprint: fingerprint)
    }

    /// Whether pairing would replace a different laptop.
    public func replaces(_ server: PairedServer?) -> Bool {
        guard let server else { return false }
        return server.fingerprint != fingerprint
    }

    /// A `.local` name, or a private, link-local or loopback address.
    public static func isLocal(_ host: String) -> Bool {
        var h = host.lowercased()
        if h.hasPrefix("["), h.hasSuffix("]") { h = String(h.dropFirst().dropLast()) }
        if let zone = h.firstIndex(of: "%") { h = String(h[..<zone]) }
        if h.hasSuffix(".") { h.removeLast() }
        if h.hasSuffix(".local") { return h.count > 6 }
        let quad = h.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
        if quad.count == 4, quad.allSatisfy({ $0 != nil && (0...255).contains($0!) }) {
            let a = quad[0]!, b = quad[1]!
            return a == 10 || a == 127 || (a == 172 && (16...31).contains(b)) || (a == 192 && b == 168)
                || (a == 169 && b == 254)
        }
        guard h.contains(":"), h.allSatisfy({ $0 == ":" || $0.isHexDigit }) else { return false }
        if h == "::1" { return true }
        guard !h.hasPrefix(":"), let first = UInt16(h.prefix(while: { $0 != ":" }), radix: 16) else { return false }
        return first & 0xffc0 == 0xfe80 || first & 0xfe00 == 0xfc00
    }

    /// The first 16 hex digits in groups of four, as `byobot serve` prints them for comparing by eye.
    public static func shortFingerprint(_ fingerprint: String) -> String {
        let head = Array(fingerprint.prefix(16))
        return stride(from: 0, to: head.count, by: 4).map { String(head[$0..<Swift.min($0 + 4, head.count)]) }
            .joined(separator: " ")
    }
}

/// A paired laptop, as the app remembers it (the token lives in the keychain, not here).
public struct PairedServer: Codable, Equatable, Hashable, Sendable {
    public var hosts: [String]
    public var port: Int
    public var fingerprint: String
    public var deviceId: String
    public var name: String
    public var pairedAt: String

    public init(hosts: [String], port: Int, fingerprint: String, deviceId: String, name: String, pairedAt: String) {
        self.hosts = hosts
        self.port = port
        self.fingerprint = fingerprint
        self.deviceId = deviceId
        self.name = name
        self.pairedAt = pairedAt
    }

    enum CodingKeys: String, CodingKey {
        case hosts, port, fingerprint, name
        case deviceId = "device_id"
        case pairedAt = "paired_at"
    }
}
