#if canImport(CryptoKit) && canImport(Compression)
import CryptoKit
import Foundation
import XCTest
@testable import ByobotKit

/// The process died mid-call. Not a URLError, so nothing retries it.
struct Killed: Error {}

/// Google's token endpoint and Drive's hidden app folder, faked at the HTTP level, as python/tests/fake_drive.py.
actor FakeDrive: HTTPSending {
    enum Fault { case status(Int), lost, kill, damage }

    struct File {
        var id: String, name: String, store: String, data: Data
    }

    var files: [String: File] = [:]
    var order: [String] = []
    var taken: Set<String> = []
    var changes: [String] = []
    var counter = 0
    var token = "access-1"
    var refreshes = 0
    var faults: [(String, Fault)] = []
    var calls: [String] = []
    var lagging: Set<String> = []  // files Drive holds that its listings don't show yet
    var signInAnswer: [String: JSONValue] = [:]

    func fail(_ at: String, _ what: Fault, times: Int = 1) { faults += Array(repeating: (at, what), count: times) }
    func clearFaults() { faults = [] }
    func setToken(_ t: String) { token = t }
    func lag() { lagging = Set(files.keys) }
    func catchUp() { lagging = [] }
    func answerSignIn(_ answer: [String: JSONValue]) { signInAnswer = answer }
    func names() -> [String] { files.values.map(\.name).sorted() }

    func meta(_ f: File) -> JSONValue {
        .object(["id": .string(f.id), "name": .string(f.name), "trashed": .bool(false),
                 "appProperties": .object(["byobot_store": .string(f.store)]),
                 "sha256Checksum": .string(SHA256.hash(data: f.data).map { String(format: "%02x", $0) }.joined())])
    }

    func reply(_ request: URLRequest, _ code: Int, _ body: Data) -> (Data, HTTPURLResponse) {
        (body, HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!)
    }

    func json(_ request: URLRequest, _ value: JSONValue, _ code: Int = 200) throws -> (Data, HTTPURLResponse) {
        reply(request, code, try JSONEncoder().encode(value))
    }

    func error(_ request: URLRequest, _ code: Int, _ reason: String) throws -> (Data, HTTPURLResponse) {
        try json(request, .object(["error": .object(["code": .number(Double(code)), "message": .string("fake"),
                                                      "errors": .array([.object(["reason": .string(reason)])])])]), code)
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let url = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        let at = "\(request.httpMethod ?? "GET") \(url.path)"
        calls.append(at)
        let fault = faults.firstIndex { at.hasPrefix($0.0) }.map { faults.remove(at: $0).1 }
        if case .status(let code)? = fault {
            return try error(request, code, code >= 500 ? "backendError" : "rateLimitExceeded")
        }
        let normal = try respond(request, url, at)
        switch fault {
        case .damage?:
            let id = changes.last!
            files[id]!.data[0] ^= 1
            return try json(request, meta(files[id]!))
        case .lost?: throw URLError(.networkConnectionLost)
        case .kill?: throw Killed()
        default: return normal
        }
    }

    func respond(_ request: URLRequest, _ url: URLComponents, _ at: String) throws -> (Data, HTTPURLResponse) {
        var params: [String: String] = [:]
        for item in url.queryItems ?? [] { params[item.name] = item.value }
        if url.path == "/token" {
            var form: [String: String] = [:]
            for item in URLComponents(string: "x:/?" + String(decoding: request.httpBody ?? Data(), as: UTF8.self))?.queryItems ?? [] {
                form[item.name] = item.value
            }
            if form["grant_type"] == "authorization_code" { return try json(request, .object(signInAnswer)) }
            XCTAssertEqual(form["refresh_token"], "refresh-1")
            XCTAssertNil(form["client_secret"])
            refreshes += 1
            token = "access-\(refreshes + 1)"
            return try json(request, .object(["access_token": .string(token), "expires_in": .number(3600)]))
        }
        guard request.value(forHTTPHeaderField: "Authorization") == "Bearer \(token)" else {
            return try error(request, 401, "authError")
        }
        let method = request.httpMethod ?? "GET"
        switch url.path {
        case "/drive/v3/changes/startPageToken":
            return try json(request, .object(["startPageToken": .string(String(changes.count))]))
        case "/drive/v3/changes":
            let start = Int(params["pageToken"]!)!
            let page = changes[start..<min(start + 2, changes.count)].filter { !lagging.contains($0) }
            let out: [JSONValue] = page.map { id in
                var c: [String: JSONValue] = ["fileId": .string(id), "removed": .bool(files[id] == nil)]
                if let f = files[id] { c["file"] = meta(f) }
                return .object(c)
            }
            var page: [String: JSONValue] = ["changes": .array(out)]
            if start + 2 < changes.count {
                page["nextPageToken"] = .string(String(start + 2))
            } else {
                page["newStartPageToken"] = .string(String(changes.count))
            }
            return try json(request, .object(page))
        case "/drive/v3/files/generateIds":
            XCTAssertEqual(params["space"], "appDataFolder")
            let ids: [JSONValue] = (0..<Int(params["count"]!)!).map { _ in counter += 1; return .string("f\(counter)") }
            return try json(request, .object(["ids": .array(ids)]))
        case "/drive/v3/files" where method == "GET":
            XCTAssertEqual(params["spaces"], "appDataFolder")
            let q = params["q"] ?? ""
            let mine = order.compactMap { files[$0] }.filter { !lagging.contains($0.id) && (q.isEmpty || q.contains("'\($0.store)'")) }
            let start = Int(params["pageToken"] ?? "0")!
            var page: [String: JSONValue] = ["files": .array(mine[min(start, mine.count)..<min(start + 3, mine.count)].map { meta($0) })]
            if start + 3 < mine.count { page["nextPageToken"] = .string(String(start + 3)) }
            return try json(request, .object(page))
        case "/upload/drive/v3/files":
            let boundary = request.value(forHTTPHeaderField: "Content-Type")!.components(separatedBy: "boundary=")[1]
            let body = request.httpBody!
            let parts = body.pieces(Data("--\(boundary)".utf8))
            let head = parts[0], content = parts[1]
            let metaData = head[head.range(of: Data("\r\n\r\n".utf8))!.upperBound...].dropLast(2)
            guard case .object(let m) = try JSONValue.parse(Data(metaData)), case .string(let id)? = m["id"],
                  case .string(let name)? = m["name"], case .object(let props)? = m["appProperties"],
                  case .string(let store)? = props["byobot_store"] else { return try error(request, 400, "badRequest") }
            XCTAssertEqual(m["parents"], .array([.string("appDataFolder")]))
            if taken.contains(id) { return try error(request, 409, "fileIdInUse") }
            taken.insert(id)
            let data = Data(content[content.range(of: Data("\r\n\r\n".utf8))!.upperBound...].dropLast(2))
            files[id] = File(id: id, name: name, store: store, data: data)
            order.append(id)
            changes.append(id)
            return try json(request, meta(files[id]!))
        default:
            let id = String(url.path.dropFirst("/drive/v3/files/".count))
            guard url.path.hasPrefix("/drive/v3/files/"), let f = files[id] else { return try error(request, 404, "notFound") }
            if method == "DELETE" {
                files[id] = nil
                changes.append(id)
                return reply(request, 204, Data())
            }
            if params["alt"] == "media" { return reply(request, 200, f.data) }
            return try json(request, meta(f))
        }
    }
}

extension Data {
    /// Splits on a run of bytes, dropping what comes before the first one.
    func pieces(_ separator: Data) -> [Data] {
        var out: [Data] = [], rest = self[...]
        while let r = rest.range(of: separator) {
            out.append(Data(rest[..<r.lowerBound]))
            rest = rest[r.upperBound...]
        }
        out.append(Data(rest))
        return Array(out.dropFirst())
    }
}

actor Naps {
    var taken: [Double] = []
    func add(_ s: Double) { taken.append(s) }
}

actor Saved {
    var tokens: GoogleTokens?
    func keep(_ t: GoogleTokens) { tokens = t }
}

final class DriveTests: XCTestCase {
    let store = "0193a0b0-0000-7000-8000-00000000c001"
    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    func signedIn() -> GoogleTokens {
        GoogleTokens(clientID: "cid.apps.googleusercontent.com", refreshToken: "refresh-1", accessToken: "access-1",
                     expiresAt: Date().addingTimeInterval(3600))
    }

    func folder(_ fake: FakeDrive, store: String? = nil, who: String = "phone", naps: Naps? = nil,
                saved: Saved? = nil, tokens: GoogleTokens? = nil) -> DriveFolder {
        let drive = Drive(tokens: tokens ?? signedIn(), http: fake, saved: { t in await saved?.keep(t) },
                          sleep: { s in await naps?.add(s) })
        return DriveFolder(storeID: store ?? self.store, drive: drive, journal: dir.appendingPathComponent("\(who).json"))
    }

    func testAFileIsWrittenOnceReadBackAndDeletedOutright() async throws {
        let fake = FakeDrive(), f = folder(fake)
        try await f.create("devices/abc.json", Data("one".utf8))
        do {
            try await f.create("devices/abc.json", Data("two".utf8))
            XCTFail("written twice")
        } catch is CloudExists {}
        let one = try await f.read("devices/abc.json"), none = try await f.read("devices/none.json")
        XCTAssertEqual(one, Data("one".utf8))
        XCTAssertNil(none)
        let names = await fake.names()
        XCTAssertEqual(names, ["\(store)/devices/abc.json"])
        try await f.delete("devices/abc.json")
        try await f.delete("devices/abc.json")
        let left = try await f.list(), files = await fake.files
        XCTAssertEqual(left, [:])
        XCTAssertTrue(files.isEmpty)
        do {
            try await f.create("../escape", Data())
            XCTFail("an unsafe path was written")
        } catch is CloudError {}
    }

    func testEachStoreSeesOnlyItsOwnFilesPageByPage() async throws {
        let fake = FakeDrive(), mine = folder(fake)
        let other = folder(fake, store: "0193a0b0-0000-7000-8000-00000000c002", who: "other")
        for n in 0..<7 { try await mine.create("devices/d\(n).json", Data("x".utf8)) }
        try await other.create("devices/d0.json", Data("y".utf8))
        let fresh = try await folder(fake, who: "fresh").list(), theirs = try await other.list()
        XCTAssertEqual(fresh.count, 7)
        XCTAssertEqual(Array(theirs.keys), ["devices/d0.json"])
    }

    func testAnotherDevicesFilesShowThroughTheChangeFeed() async throws {
        let fake = FakeDrive(), mac = folder(fake, who: "mac"), phone = folder(fake)
        _ = try await mac.list()
        var changed = try await mac.changed()
        XCTAssertFalse(changed)
        for n in 0..<5 { try await phone.create("segments/p/\(n).seg", Data("set".utf8)) }
        let before = await fake.calls.count
        changed = try await mac.changed()
        XCTAssertTrue(changed)
        let listed = try await mac.list(), calls = await fake.calls
        XCTAssertEqual(listed.count, 5)
        XCTAssertFalse(calls[before...].contains("GET /drive/v3/files"))  // the feed, not a full listing
        try await phone.delete("segments/p/0.seg")
        let after = try await mac.list()
        XCTAssertNil(after["segments/p/0.seg"])
    }

    func testABusyDriveIsRetriedWithBackoff() async throws {
        for code in [429, 500, 503] {
            let fake = FakeDrive(), naps = Naps(), f = folder(fake, naps: naps)
            await fake.fail("GET /drive/v3/files", .status(code), times: 2)
            try await f.create("devices/abc.json", Data("one".utf8))
            let taken = await naps.taken, listed = try await f.list()
            XCTAssertEqual(taken.count, 2)
            XCTAssertGreaterThan(taken[1], taken[0])
            XCTAssertEqual(Array(listed.keys), ["devices/abc.json"])
        }
    }

    func testDriveDownForGoodFailsTheCall() async throws {
        let fake = FakeDrive(), f = folder(fake)
        await fake.fail("GET /drive/v3/changes/startPageToken", .status(503), times: Drive.tries)
        do {
            _ = try await f.list()
            XCTFail("Drive was down")
        } catch DriveError.upstream(let why) {
            XCTAssertTrue(why.contains("503"))
        }
    }

    func testAnExpiredTokenIsRefreshedAndSaved() async throws {
        let fake = FakeDrive(), saved = Saved(), f = folder(fake, saved: saved)
        await fake.setToken("access-elsewhere")  // Google expired it early
        try await f.create("devices/abc.json", Data("one".utf8))
        let refreshes = await fake.refreshes, token = await fake.token, kept = await saved.tokens
        XCTAssertEqual(refreshes, 1)
        XCTAssertEqual(kept?.accessToken, token)
        XCTAssertEqual(kept?.refreshToken, "refresh-1")
    }

    func testARevokedSignInSaysToSignInAgain() async throws {
        struct Revoked: HTTPSending {
            func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
                (Data(#"{"error":"invalid_grant","error_description":"Token has been expired or revoked."}"#.utf8),
                 HTTPURLResponse(url: request.url!, statusCode: 400, httpVersion: nil, headerFields: nil)!)
            }
        }
        var tokens = signedIn()
        tokens.expiresAt = .distantPast
        let f = DriveFolder(storeID: store, drive: Drive(tokens: tokens, http: Revoked()),
                            journal: dir.appendingPathComponent("j.json"))
        do {
            _ = try await f.list()
            XCTFail("signed out")
        } catch DriveError.notConnected(let why) {
            XCTAssertTrue(why.contains("Sign in again"))
        }
    }

    func testAnUploadWhoseAnswerIsLostLandsOnce() async throws {
        let fake = FakeDrive(), f = folder(fake)
        await fake.fail("POST /upload", .lost)
        try await f.create("devices/abc.json", Data("one".utf8))
        let names = await fake.names(), read = try await f.read("devices/abc.json")
        XCTAssertEqual(names, ["\(store)/devices/abc.json"])
        XCTAssertEqual(read, Data("one".utf8))
    }

    func testAnUploadCutShortByACrashIsFinishedWithTheSameId() async throws {
        let fake = FakeDrive()
        await fake.fail("POST /upload", .kill)
        do {
            try await folder(fake).create("devices/abc.json", Data("one".utf8))
            XCTFail("killed")
        } catch is Killed {}
        await fake.lag()
        try await folder(fake).create("devices/abc.json", Data("one".utf8))  // the next run, before listings show it
        let names = await fake.names()
        XCTAssertEqual(names, ["\(store)/devices/abc.json"])
        await fake.catchUp()
        let read = try await folder(fake, who: "fresh").read("devices/abc.json")
        XCTAssertEqual(read, Data("one".utf8))
    }

    func testADamagedUploadIsDeletedAndSentAgain() async throws {
        let fake = FakeDrive(), f = folder(fake)
        await fake.fail("POST /upload", .damage)
        do {
            try await f.create("devices/abc.json", Data("one".utf8))
            XCTFail("damaged")
        } catch DriveError.upstream(let why) {
            XCTAssertTrue(why.contains("damaged"))
        }
        let left = try await f.list()
        XCTAssertEqual(left, [:])
        try await f.create("devices/abc.json", Data("one".utf8))
        let read = try await f.read("devices/abc.json")
        XCTAssertEqual(read, Data("one".utf8))
    }

    func testThePhoneRelaysThroughDrive() async throws {
        let k = try CloudRelayTests.keeper(), fake = FakeDrive(), f = folder(fake, store: k.store)
        var relay = try await CloudRelay.join(f, link: k.link, deviceID: k.relayID, identity: k.relay)
        let keeperSide = folder(fake, store: k.store, who: "mac")
        try await keeperSide.create(Cloud.epochPath(1), k.epochFile)
        try await keeperSide.create(Cloud.keyPath(1, k.relayID), k.keyFile)
        relay.send(SampleBatch(samples: [Sample(source: "apple_health", sourceId: "W1",
                                                type: "HKQuantityTypeIdentifierBodyMass",
                                                start: "2026-11-23T06:30:00-05:00", value: .number(80), unit: "kg")]))
        let report = try await relay.sync(f, identity: k.relay)
        XCTAssertEqual(report.epoch, 1)
        XCTAssertEqual(report.written, 1)
        let path = Cloud.segmentPath(kind: "segment", epoch: 1, seq: 1, deviceID: k.relayID)
        let data = try await keeperSide.read(path)
        let found = try Cloud.readSegment(data ?? Data(), path: path, storeID: k.store, key: k.epochKey,
                                          signPublic: k.relay.signPublic)
        XCTAssertNotNil(found.messages.first?["samples"])
    }

    // MARK: sign-in

    func testTheSignInAsksForTheAppFolderAloneWithPKCE() throws {
        let s = Google.signIn(clientID: "123-abc.apps.googleusercontent.com")
        let items = URLComponents(url: s.url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ n: String) -> String? { items.first { $0.name == n }?.value }
        XCTAssertEqual(s.callbackScheme, "com.googleusercontent.apps.123-abc")
        XCTAssertEqual(value("redirect_uri"), "com.googleusercontent.apps.123-abc:/oauth2redirect")
        XCTAssertEqual(value("scope"), Google.scope)
        XCTAssertEqual(value("code_challenge_method"), "S256")
        XCTAssertEqual(value("code_challenge"),
                       Google.base64url(Data(SHA256.hash(data: Data(s.verifier.utf8)))))
        XCTAssertEqual(value("state"), s.state)
    }

    func finish(_ answer: [String: JSONValue], state: String? = nil) async throws -> GoogleTokens {
        let fake = FakeDrive()
        await fake.answerSignIn(answer)
        let s = Google.signIn(clientID: "123-abc.apps.googleusercontent.com")
        let callback = URL(string: "\(s.callbackScheme):/oauth2redirect?state=\(state ?? s.state)&code=4%2Fcode")!
        return try await Google.finish(s, callback: callback, http: fake)
    }

    let good: [String: JSONValue] = ["access_token": .string("access-1"), "refresh_token": .string("refresh-1"),
                                     "expires_in": .number(3599), "scope": .string(Google.scope)]

    func testASignInKeepsItsTokens() async throws {
        let tokens = try await finish(good)
        XCTAssertEqual(tokens.refreshToken, "refresh-1")
        XCTAssertEqual(tokens.clientID, "123-abc.apps.googleusercontent.com")
    }

    func testASignInIsRefusedForAProjectInTestingOrWithMoreScopes() async throws {
        var testing = good
        testing["refresh_token_expires_in"] = .number(604_799)
        var wider = good
        wider["scope"] = .string("\(Google.scope) https://www.googleapis.com/auth/drive")
        for answer in [testing, wider] {
            do {
                _ = try await finish(answer)
                XCTFail("signed in")
            } catch DriveError.upstream {}
        }
        do {
            _ = try await finish(good, state: "other")
            XCTFail("signed in with another request's answer")
        } catch DriveError.upstream {}
    }
}
#endif
