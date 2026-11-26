// Cloud protocol 1's files as the phone reads and writes them: segments, signed documents, sealed epoch keys
// and its device file. The same rules as python/src/byobot/cloud, checked against schema/fixtures/v1/cloud.json.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

#if canImport(CryptoKit) && canImport(Compression)
import Compression
import CryptoKit
import Foundation

public struct CloudError: Error, Equatable, CustomStringConvertible, LocalizedError {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
    public var errorDescription: String? { message }
}

/// A device's two key pairs: one to receive sealed keys (X25519), one to sign what it writes (Ed25519).
public struct CloudIdentity {
    public let box: Curve25519.KeyAgreement.PrivateKey
    public let sign: Curve25519.Signing.PrivateKey

    public init(box: Curve25519.KeyAgreement.PrivateKey = .init(), sign: Curve25519.Signing.PrivateKey = .init()) {
        self.box = box
        self.sign = sign
    }

    public init(privateBox: Data, privateSign: Data) throws {
        guard let box = try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: privateBox),
              let sign = try? Curve25519.Signing.PrivateKey(rawRepresentation: privateSign) else {
            throw CloudError("not a valid key or ciphertext")
        }
        self.init(box: box, sign: sign)
    }

    public var boxPublic: String { box.publicKey.rawRepresentation.base64EncodedString() }
    public var signPublic: String { sign.publicKey.rawRepresentation.base64EncodedString() }
}

public struct SegmentName: Equatable {
    public let kind: String
    public let epoch: Int
    public let seq: Int
    public let deviceID: String?
}

public enum Cloud {
    public static let format = 1
    public static let messages: Set<String> = ["events", "samples", "plan", "receipts"]
    static let kinds: Set<String> = ["segment", "checkpoint", "plan"]
    static let magic = "byobot-segment"
    static let segmentDomain = Data("byobot-cloud segment 1\n".utf8)
    static let documentDomain = Data("byobot-cloud document 1\n".utf8)
    static let sealInfo = Data("byobot-cloud seal 1".utf8)
    static let joinDomain = "byobot-cloud join 1"
    static let maxHeader = 1024
    static let maxBody = 32 * 1024 * 1024
    static let maxSeq = 9_999_999_999
    static let maxEpoch = 9999

    // MARK: names

    public static func segmentPath(kind: String, epoch: Int, seq: Int, deviceID: String? = nil) -> String {
        switch kind {
        case "segment": return "segments/\(deviceID ?? "")/\(pad(epoch, 4))-\(pad(seq, 10)).seg"
        case "checkpoint": return "checkpoints/\(pad(epoch, 4))/\(pad(seq, 10)).seg"
        default: return "plan/\(pad(epoch, 4))-\(pad(seq, 10)).seg"
        }
    }

    public static func epochPath(_ epoch: Int) -> String { "epochs/\(pad(epoch, 4)).json" }
    public static func devicePath(_ deviceID: String) -> String { "devices/\(deviceID).json" }
    public static func keyPath(_ epoch: Int, _ deviceID: String) -> String { "keys/\(pad(epoch, 4))/\(deviceID).key" }

    /// What a segment's path says its header holds, or nil if the path isn't a segment's.
    public static func parseName(_ path: String) -> SegmentName? {
        let parts = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        func numbered(_ name: String, _ first: Int?) -> (Int, Int)? {
            guard name.hasSuffix(".seg") else { return nil }
            let stem = String(name.dropLast(4))
            if let first {
                guard let seq = digits(stem, 10) else { return nil }
                return (first, seq)
            }
            let halves = stem.split(separator: "-", omittingEmptySubsequences: false).map(String.init)
            guard halves.count == 2, let epoch = digits(halves[0], 4), let seq = digits(halves[1], 10) else { return nil }
            return (epoch, seq)
        }
        if parts.count == 3, parts[0] == "segments", isID(parts[1]), case let (epoch, seq)? = numbered(parts[2], nil) {
            return SegmentName(kind: "segment", epoch: epoch, seq: seq, deviceID: parts[1])
        }
        if parts.count == 3, parts[0] == "checkpoints", let epoch = digits(parts[1], 4),
           case let (_, seq)? = numbered(parts[2], epoch) {
            return SegmentName(kind: "checkpoint", epoch: epoch, seq: seq, deviceID: nil)
        }
        if parts.count == 2, parts[0] == "plan", case let (epoch, seq)? = numbered(parts[1], nil) {
            return SegmentName(kind: "plan", epoch: epoch, seq: seq, deviceID: nil)
        }
        return nil
    }

    // MARK: segments

    /// A segment's header, not yet verified.
    public static func header(_ data: Data) throws -> [String: JSONValue] {
        let bytes = [UInt8](data)
        guard let end = bytes.prefix(maxHeader).firstIndex(of: 0x0A), end > 0,
              case .object(let found)? = try? JSONValue.parse(Data(bytes[..<end])),
              found["magic"] == .string(magic) else {
            throw CloudError("not a byobot segment")
        }
        guard found["format"] == .number(Double(format)) else {
            throw CloudError("written by a newer byobot; upgrade byobot")
        }
        guard let kind = text(found, "kind"), kinds.contains(kind), text(found, "store_id") != nil,
              text(found, "device_id") != nil, int(found["epoch"]) != nil, int(found["seq"]) != nil else {
            throw CloudError("not a byobot segment")
        }
        return found
    }

    /// A segment's header and messages, once its place, store, signature, key and length all check out.
    public static func readSegment(_ data: Data, path: String, storeID: String, key: Data,
                                   signPublic: String) throws -> (header: [String: JSONValue], messages: [[String: JSONValue]]) {
        let found = try header(data)
        guard let expected = parseName(path), found["kind"] == .string(expected.kind),
              int(found["epoch"]) == expected.epoch, int(found["seq"]) == expected.seq,
              expected.deviceID == nil || text(found, "device_id") == expected.deviceID else {
            throw CloudError("moved or renamed")
        }
        guard text(found, "store_id") == storeID else { throw CloudError("from another store") }
        let bytes = [UInt8](data)
        let split = bytes.firstIndex(of: 0x0A)! + 1
        guard bytes.count >= split + 16 + 64 else { throw CloudError("cut short") }
        let head = Data(bytes[..<split])
        let ciphertext = Data(bytes[split..<(bytes.count - 64)])
        let signature = Data(bytes[(bytes.count - 64)...])
        try verify(signPublic, segmentDomain, head + ciphertext, signature)
        let nonce = try unb64(found["nonce"], size: 12)
        let body = try open(key, nonce: nonce, ciphertext: ciphertext, aad: head,
                            failure: "can't be opened with this key, or changed")
        return (found, try lines(body))
    }

    public static func writeSegment(kind: String, storeID: String, deviceID: String, epoch: Int, seq: Int,
                                    messages: [JSONValue], key: Data, signer: CloudIdentity,
                                    nonce: Data? = nil) throws -> Data {
        guard kinds.contains(kind), (0...maxEpoch).contains(epoch), (0...maxSeq).contains(seq) else {
            throw CloudError("not a segment kind, epoch or seq")
        }
        let nonce = nonce ?? Data((0..<12).map { _ in UInt8.random(in: 0...255) })
        let head = try dumps(.object([
            "magic": .string(magic), "format": .number(Double(format)), "kind": .string(kind),
            "store_id": .string(storeID), "device_id": .string(deviceID), "epoch": .number(Double(epoch)),
            "seq": .number(Double(seq)), "nonce": .string(nonce.base64EncodedString()),
        ])) + Data("\n".utf8)
        let end: JSONValue = .object(["end": .object(["lines": .number(Double(messages.count))])])
        var plain = Data()
        for line in messages + [end] { plain += try dumps(line) + Data("\n".utf8) }
        let ciphertext = try seal(key, nonce: nonce, plain: try gzip(plain), aad: head)
        let signature = try signer.sign.signature(for: segmentDomain + head + ciphertext)
        return head + ciphertext + signature
    }

    static func lines(_ body: Data) throws -> [[String: JSONValue]] {
        guard let text = String(data: try gunzip(body), encoding: .utf8) else {
            throw CloudError("not a readable segment body")
        }
        var values: [JSONValue] = []
        for line in text.components(separatedBy: "\n").dropLast() {
            guard let value = try? JSONValue.parse(Data(line.utf8)) else {
                throw CloudError("not a readable segment body")
            }
            values.append(value)
        }
        guard case .object(let last)? = values.last, last.count == 1,
              last["end"] == .object(["lines": .number(Double(values.count - 1))]) else {
            throw CloudError("cut short")
        }
        return try values.dropLast().map { value -> [String: JSONValue] in
            guard case .object(let message) = value, message.count == 1, let name = message.keys.first,
                  messages.contains(name) else {
                throw CloudError("holds a message this byobot doesn't know; upgrade byobot")
            }
            return message
        }
    }

    // MARK: signed documents and files

    public static func readDocument(_ data: Data, signPublic: String) throws -> [String: JSONValue] {
        guard case .object(let doc)? = try? JSONValue.parse(data), doc["body"] != nil else {
            throw CloudError("not a byobot cloud file")
        }
        guard doc["format"] == .number(Double(format)) else {
            throw CloudError("written by a newer byobot; upgrade byobot")
        }
        let raw = try unb64(doc["body"])
        try verify(signPublic, documentDomain, raw, try unb64(doc["sig"], size: 64))
        guard case .object(let body)? = try? JSONValue.parse(raw) else { throw CloudError("not a byobot cloud file") }
        return body
    }

    public static func readEpoch(_ data: Data, keeperSign: String, storeID: String,
                                 epoch: Int) throws -> [String: JSONValue] {
        var body = try readDocument(data, signPublic: keeperSign)
        try matches(body, storeID, epoch)
        guard case .array(let devices)? = body["devices"], int(body["checkpoint"]) != nil,
              text(body, "keeper") != nil else {
            throw CloudError("not a byobot epoch")
        }
        body["devices"] = .array(try devices.map { .object(try checkDevice($0)) })
        return body
    }

    /// This device's epoch key, from a key file the keeper signed and sealed to it.
    public static func openKey(_ data: Data, me: CloudIdentity, deviceID: String, keeperSign: String,
                               storeID: String, epoch: Int) throws -> Data {
        let body = try readDocument(data, signPublic: keeperSign)
        try matches(body, storeID, epoch)
        let device = try checkDevice(body["device"])
        guard device["id"] == .string(deviceID), device["box"] == .string(me.boxPublic) else {
            throw CloudError("sealed to another device")
        }
        guard case .object(let sealed)? = body["sealed"] else { throw CloudError("not a valid key or ciphertext") }
        let key = try unseal(me, sealed, context: Data("byobot-cloud key 1\n\(storeID)\n\(epoch)\n\(deviceID)".utf8))
        guard key.count == 32 else { throw CloudError("not an epoch key") }
        return key
    }

    public static func openRecovery(_ data: Data, recoveryKey: Data, storeID: String,
                                    epoch: Int) throws -> [String: JSONValue] {
        guard case .object(let body)? = try? JSONValue.parse(data) else {
            throw CloudError("not a byobot recovery key")
        }
        try matches(body, storeID, epoch)
        do {
            let plain = try open(recoveryKey, nonce: try unb64(body["nonce"], size: 12),
                                 ciphertext: try unb64(body["ciphertext"]),
                                 aad: Data("byobot-cloud recovery 1\n\(storeID)\n\(epoch)".utf8), failure: "")
            guard case .object(let content) = try JSONValue.parse(plain) else { throw CloudError("") }
            return content
        } catch {
            throw CloudError("that recovery code doesn't open this store")
        }
    }

    /// A device's public keys, plain. A relay's carries its join proof.
    public static func deviceFile(storeID: String, id: String, deviceClass: String, identity: CloudIdentity,
                                  proof: String? = nil) throws -> Data {
        var body: [String: JSONValue] = try checkDevice(.object([
            "id": .string(id), "class": .string(deviceClass), "box": .string(identity.boxPublic),
            "sign": .string(identity.signPublic),
        ]))
        body["format"] = .number(Double(format))
        body["store_id"] = .string(storeID)
        if let proof { body["proof"] = .string(proof) }
        return try dumps(.object(body))
    }

    public static func readDevice(_ data: Data, storeID: String, deviceID: String) throws -> [String: JSONValue] {
        guard case .object(let body)? = try? JSONValue.parse(data) else { throw CloudError("not a byobot device") }
        guard body["format"] == .number(Double(format)) else {
            throw CloudError("not a byobot device, or written by a newer byobot")
        }
        guard text(body, "store_id") == storeID else { throw CloudError("from another store") }
        var device = try checkDevice(.object(body))
        guard device["id"] == .string(deviceID) else { throw CloudError("moved or renamed") }
        if let proof = text(body, "proof") { device["proof"] = .string(proof) }
        return device
    }

    /// What a joining device shows it holds the one-time join secret: an HMAC over who it says it is.
    public static func joinProof(secret: Data, storeID: String, deviceID: String, identity: CloudIdentity) -> String {
        let message = [joinDomain, storeID, deviceID, identity.boxPublic, identity.signPublic].joined(separator: "\n")
        let mac = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: SymmetricKey(data: secret))
        return Data(mac).base64EncodedString()
    }

    // MARK: helpers

    static func checkDevice(_ value: JSONValue?) throws -> [String: JSONValue] {
        guard case .object(let device)? = value, let id = text(device, "id"), isID(id), id != "recovery",
              let cls = text(device, "class"), ["keeper", "relay"].contains(cls) else {
            throw CloudError("not a byobot device")
        }
        _ = try unb64(device["box"], size: 32)
        _ = try unb64(device["sign"], size: 32)
        return ["id": .string(id), "class": .string(cls), "box": device["box"]!, "sign": device["sign"]!]
    }

    static func matches(_ body: [String: JSONValue], _ storeID: String, _ epoch: Int) throws {
        guard body["format"] == .number(Double(format)) else {
            throw CloudError("written by a newer byobot; upgrade byobot")
        }
        guard text(body, "store_id") == storeID else { throw CloudError("from another store") }
        guard int(body["epoch"]) == epoch else { throw CloudError("moved or renamed") }
    }

    static func verify(_ signPublic: String, _ domain: Data, _ data: Data, _ signature: Data) throws {
        let raw = try unb64(.string(signPublic), size: 32)
        guard let key = try? Curve25519.Signing.PublicKey(rawRepresentation: raw),
              key.isValidSignature(signature, for: domain + data) else {
            throw CloudError("changed or cut short since its writer signed it, or signed by another device")
        }
    }

    static func unseal(_ me: CloudIdentity, _ sealed: [String: JSONValue], context: Data) throws -> Data {
        let ephemeral = try unb64(sealed["ephemeral"], size: 32)
        guard let theirs = try? Curve25519.KeyAgreement.PublicKey(rawRepresentation: ephemeral),
              let shared = try? me.box.sharedSecretFromKeyAgreement(with: theirs) else {
            throw CloudError("sealed to another device, or changed")
        }
        let key = shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: ephemeral + me.box.publicKey.rawRepresentation,
                                                 sharedInfo: sealInfo, outputByteCount: 32)
        return try open(key.withUnsafeBytes { Data($0) }, nonce: Data(count: 12),
                        ciphertext: try unb64(sealed["ciphertext"]), aad: context,
                        failure: "sealed to another device, or changed")
    }

    static func open(_ key: Data, nonce: Data, ciphertext: Data, aad: Data, failure: String) throws -> Data {
        guard key.count == 32, ciphertext.count >= 16,
              let box = try? ChaChaPoly.SealedBox(combined: nonce + ciphertext),
              let plain = try? ChaChaPoly.open(box, using: SymmetricKey(data: key), authenticating: aad) else {
            throw CloudError(failure)
        }
        return plain
    }

    static func seal(_ key: Data, nonce: Data, plain: Data, aad: Data) throws -> Data {
        let box = try ChaChaPoly.seal(plain, using: SymmetricKey(data: key), nonce: ChaChaPoly.Nonce(data: nonce),
                                      authenticating: aad)
        return box.ciphertext + box.tag
    }

    /// JSON as Python's `dumps`: sorted keys, no spaces, slashes as they are.
    static func dumps(_ value: JSONValue) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(value)
    }

    static func unb64(_ value: JSONValue?, size: Int? = nil) throws -> Data {
        guard case .string(let text)? = value, let data = Data(base64Encoded: text),
              size == nil || data.count == size else {
            throw CloudError("not a valid key or ciphertext")
        }
        return data
    }

    static func text(_ object: [String: JSONValue], _ key: String) -> String? {
        if case .string(let s)? = object[key] { return s }
        return nil
    }

    static func int(_ value: JSONValue?) -> Int? {
        guard case .number(let n)? = value, n.rounded() == n, abs(n) < 1e15 else { return nil }
        return Int(n)
    }

    static func pad(_ n: Int, _ width: Int) -> String {
        let s = String(n)
        return String(repeating: "0", count: max(0, width - s.count)) + s
    }

    static func digits(_ s: String, _ count: Int) -> Int? {
        guard s.utf8.count == count, s.utf8.allSatisfy({ (0x30...0x39).contains($0) }) else { return nil }
        return Int(s)
    }

    static func isID(_ s: String) -> Bool {
        let bytes = Array(s.utf8)
        func ok(_ b: UInt8) -> Bool {
            (0x30...0x39).contains(b) || (0x41...0x5A).contains(b) || (0x61...0x7A).contains(b)
        }
        guard (1...128).contains(bytes.count), ok(bytes[0]) else { return false }
        return bytes.dropFirst().allSatisfy { ok($0) || [0x2E, 0x5F, 0x3A, 0x2D].contains($0) }
    }

    // MARK: gzip

    static func gzip(_ data: Data) throws -> Data {
        let deflated = try stream(Array(data), operation: COMPRESSION_STREAM_ENCODE).output
        var out = Data([0x1F, 0x8B, 8, 0, 0, 0, 0, 0, 0, 0xFF])
        out += deflated
        out += le32(crc32(Array(data))) + le32(UInt32(truncatingIfNeeded: data.count))
        return out
    }

    static func gunzip(_ data: Data) throws -> Data {
        let b = [UInt8](data)
        let unreadable = CloudError("not a readable segment body")
        guard b.count >= 18, b[0] == 0x1F, b[1] == 0x8B, b[2] == 8 else { throw unreadable }
        let flags = b[3]
        var i = 10
        if flags & 4 != 0 { i += 2 + (Int(b[i]) | Int(b[i + 1]) << 8) }
        for flag in [UInt8(8), 16] where flags & flag != 0 {
            while i < b.count, b[i] != 0 { i += 1 }
            i += 1
        }
        if flags & 2 != 0 { i += 2 }
        guard i + 8 <= b.count else { throw unreadable }
        let (output, left) = try stream(Array(b[i..<(b.count - 8)]), operation: COMPRESSION_STREAM_DECODE)
        guard left == 0 else { throw CloudError("cut short, or too large") }
        let tail = b.count - 8
        let crc = UInt32(b[tail]) | UInt32(b[tail + 1]) << 8 | UInt32(b[tail + 2]) << 16 | UInt32(b[tail + 3]) << 24
        let size = UInt32(b[tail + 4]) | UInt32(b[tail + 5]) << 8 | UInt32(b[tail + 6]) << 16 | UInt32(b[tail + 7]) << 24
        guard crc32(Array(output)) == crc, UInt32(truncatingIfNeeded: output.count) == size else { throw unreadable }
        return output
    }

    /// Raw deflate in or out; for decoding, also how many input bytes were left after the stream's end.
    static func stream(_ input: [UInt8], operation: compression_stream_operation) throws -> (output: Data, left: Int) {
        let unreadable = CloudError("not a readable segment body")
        let stream = UnsafeMutablePointer<compression_stream>.allocate(capacity: 1)
        defer { stream.deallocate() }
        guard compression_stream_init(stream, operation, COMPRESSION_ZLIB) == COMPRESSION_STATUS_OK else {
            throw unreadable
        }
        defer { compression_stream_destroy(stream) }
        let size = 64 * 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
        defer { buffer.deallocate() }
        var output = Data()
        return try input.withUnsafeBufferPointer { src -> (output: Data, left: Int) in
            let empty: [UInt8] = [0]
            return try empty.withUnsafeBufferPointer { none -> (output: Data, left: Int) in
                stream.pointee.src_ptr = src.baseAddress ?? none.baseAddress!
                stream.pointee.src_size = src.count
                while true {
                    stream.pointee.dst_ptr = buffer
                    stream.pointee.dst_size = size
                    let status = compression_stream_process(stream, Int32(COMPRESSION_STREAM_FINALIZE.rawValue))
                    let produced = size - stream.pointee.dst_size
                    output.append(buffer, count: produced)
                    guard output.count <= maxBody else { throw CloudError("cut short, or too large") }
                    if status == COMPRESSION_STATUS_END {
                        return (output, stream.pointee.src_size)
                    }
                    guard status == COMPRESSION_STATUS_OK else { throw unreadable }
                    if produced == 0, stream.pointee.src_size == 0 { throw CloudError("cut short, or too large") }
                }
            }
        }
    }

    static let crcTable: [UInt32] = (0..<256).map { i -> UInt32 in
        var c = UInt32(i)
        for _ in 0..<8 { c = c & 1 != 0 ? 0xEDB8_8320 ^ (c >> 1) : c >> 1 }
        return c
    }

    static func crc32(_ bytes: [UInt8]) -> UInt32 {
        var c: UInt32 = 0xFFFF_FFFF
        for b in bytes { c = crcTable[Int((c ^ UInt32(b)) & 0xFF)] ^ (c >> 8) }
        return c ^ 0xFFFF_FFFF
    }

    static func le32(_ n: UInt32) -> Data {
        Data([UInt8(n & 0xFF), UInt8(n >> 8 & 0xFF), UInt8(n >> 16 & 0xFF), UInt8(n >> 24 & 0xFF)])
    }
}
#endif
