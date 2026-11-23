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

    /// Parse `byobot://pair?v=0&host=...&host=...&port=8765&code=123456&fp=<base64url>`.
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
}
