// Secrets kept in the keychain on this device only: the laptop's token, the Google sign-in and the cloud keys.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation
import Security

enum Keychain {
    static let service = "byobot.sync-token"

    static func setToken(_ token: String, for server: String) throws {
        try set(Data(token.utf8), service: service, account: server)
    }

    static func token(for server: String) -> String? {
        data(service: service, account: server).flatMap { String(data: $0, encoding: .utf8) }
    }

    static func deleteToken(for server: String) {
        delete(service: service, account: server)
    }

    static func set(_ data: Data, service: String, account: String) throws {
        delete(service: service, account: account)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }

    static func data(service: String, account: String) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess else { return nil }
        return item as? Data
    }

    static func delete(service: String, account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
