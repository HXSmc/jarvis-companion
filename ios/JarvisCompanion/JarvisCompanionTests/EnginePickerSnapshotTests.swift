import SwiftUI
import UIKit
import XCTest
@testable import JarvisCompanion

/// The engine picker row (EnginePickerRow.swift): every state drawn in a real
/// simulator window to PNG ($CARD_SHOTS_DIR, set by CI through
/// TEST_RUNNER_CARD_SHOTS_DIR) for the owner's before/after page. No network:
/// the shared store is seeded and the refresh is skipped.
@MainActor
final class EnginePickerSnapshotTests: XCTestCase {

    static let glm = EngineOption(id: "glm", label: "GLM", kind: "worker", available: true, isDefault: false, reason: nil)
    static let glmDown = EngineOption(id: "glm", label: "GLM", kind: "glm", available: false, isDefault: false,
                                      reason: "endpoint offline")
    static let agy = EngineOption(id: "agy", label: "Antigravity", kind: "worker", available: true, isDefault: false, reason: nil)
    static let agyDown = EngineOption(id: "agy", label: "Antigravity", kind: "worker", available: false, isDefault: false,
                                      reason: "signed out")

    func testEngineRowStates() throws {
        EngineStore.shared.seed([.hub, Self.glm, Self.agy])
        try shot("11-engine-row-default",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(false), refreshOnAppear: false))
        try shot("12-engine-row-glm-on",
                 EnginePickerRow(engineID: .constant("glm"), newSession: .constant(true), refreshOnAppear: false))
        try shot("13-engine-row-locked",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(false),
                                 lockedEngine: "agy", refreshOnAppear: false))
        try shot("14-engine-row-ax5",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(true), refreshOnAppear: false)
                     .dynamicTypeSize(.accessibility5))
        // the picked engine went down: warning on the pill, sends blocked (never a silent Hub)
        EngineStore.shared.seed([.hub, Self.glmDown, Self.agy])
        try shot("15-engine-row-unavailable",
                 EnginePickerRow(engineID: .constant("glm"), newSession: .constant(false), refreshOnAppear: false))
        // a long label is never cut: the row gives it more than half, or stacks when both cannot fit
        EngineStore.shared.seed([.hub, Self.glm, Self.agyDown])
        try shot("16-engine-row-long-label",
                 EnginePickerRow(engineID: .constant("agy"), newSession: .constant(true), refreshOnAppear: false))
        EngineStore.shared.seed([.hub, Self.glmDown, Self.agy])
        XCTAssertEqual(EngineStore.shared.unavailableNote("glm"), "GLM unavailable: endpoint offline")
        XCTAssertNil(EngineStore.shared.unavailableNote("hub"))
        XCTAssertNil(EngineStore.shared.unavailableNote("agy"))
    }

    // MARK: local-only engine (router-local-model)

    static let local = EngineOption(id: "qwen-local", label: "Qwen 27B · Local only", kind: "local", available: true,
                                    isDefault: false, reason: nil, localOnly: true)
    static let localBare = EngineOption(id: "qwen-local", label: "Qwen 27B", kind: "local", available: true,
                                        isDefault: false, reason: nil, localOnly: true)
    static let localDown = EngineOption(id: "qwen-local", label: "Qwen 27B · Local only", kind: "local", available: false,
                                        isDefault: false, reason: "gaming PC asleep", localOnly: true)
    static let busy = "Local model unavailable: gaming PC asleep. Nothing was sent to any other model."
    static let wrongChat = "This chat belongs to Hub. Start a new chat to use the local model."

    func testLocalOnlyRow() throws {
        EngineStore.shared.seed([.hub, Self.glm, Self.local])
        try shot("21-engine-row-local",
                 EnginePickerRow(engineID: .constant("qwen-local"), newSession: .constant(false), refreshOnAppear: false))
        try shot("22-engine-row-local-locked",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(false),
                                 lockedEngine: "qwen-local", refreshOnAppear: false))
        // a local session whose engine the cached list no longer has: still says local only
        EngineStore.shared.seed([.hub, Self.glm])
        try shot("23-engine-row-local-locked-unlisted",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(false),
                                 lockedEngine: "qwen-local", lockedLocalOnly: true, refreshOnAppear: false))
        EngineStore.shared.seed([.hub, Self.glm, Self.localDown])
        try shot("24-engine-row-local-unavailable",
                 EnginePickerRow(engineID: .constant("qwen-local"), newSession: .constant(false), refreshOnAppear: false))
        // "Local only" once, whether or not the label already has it
        XCTAssertEqual(Self.local.displayLabel, "Qwen 27B · Local only")
        XCTAssertEqual(Self.localBare.displayLabel, "Qwen 27B · Local only")
        XCTAssertEqual(Self.local.spokenLabel, "Qwen 27B, local only, nothing leaves your devices")
        XCTAssertEqual(Self.localBare.spokenLabel, "Qwen 27B, local only, nothing leaves your devices")
        XCTAssertEqual(Self.localDown.displayLabel, "Qwen 27B")
        XCTAssertEqual(Self.glm.displayLabel, "GLM")
        XCTAssertEqual(Self.glm.spokenLabel, "GLM")
    }

    func testDecodeWithoutLocalOnlyField() throws {
        let old = #"[{"id":"glm","label":"GLM","kind":"worker","available":true,"default":false,"reason":null}]"#
        let new = #"[{"id":"qwen-local","label":"Qwen 27B · Local only","kind":"local","available":true,"default":false,"local_only":true}]"#
        let oldList = try JSONDecoder().decode([EngineOption].self, from: Data(old.utf8))
        let newList = try JSONDecoder().decode([EngineOption].self, from: Data(new.utf8))
        XCTAssertEqual(oldList.first?.localOnly, false)
        XCTAssertEqual(newList.first?.localOnly, true)
        XCTAssertNil(newList.first?.reason)
        // the UserDefaults cache sendChoice reads keeps the flag
        let cached = try JSONDecoder().decode([EngineOption].self, from: JSONEncoder().encode(newList))
        XCTAssertEqual(cached, newList)
    }

    func testUpstreamNotes() {
        XCTAssertEqual(EngineStore.upstreamNote("gaming-pc"), "Gaming PC")
        XCTAssertEqual(EngineStore.upstreamNote("mini-pc"), "Mini PC · Slower: running on the mini PC")
        XCTAssertNil(EngineStore.upstreamNote("unknown"))
        XCTAssertNil(EngineStore.upstreamNote(nil))
    }

    func testQuickAskNeverForLocalPick() throws {
        try useStubGateway(list: [.hub, Self.glm, Self.local], pick: "hub")
        XCTAssertTrue(EngineStore.usesQuickAsk("?weather"))
        XCTAssertFalse(EngineStore.usesQuickAsk("weather"))
        UserDefaults.standard.set("glm", forKey: "engineID")
        XCTAssertTrue(EngineStore.usesQuickAsk("?weather"))
        UserDefaults.standard.set("qwen-local", forKey: "engineID")
        XCTAssertFalse(EngineStore.usesQuickAsk("?weather"))
        UserDefaults.standard.set("gone", forKey: "engineID")  // unlisted: never the cloud /ask
        XCTAssertFalse(EngineStore.usesQuickAsk("?weather"))
        // S4: a gateway without `local_only` still counts a "Local only" label as local
        let unflagged = EngineOption(id: "qwen-local", label: "Qwen 27B · Local only", kind: "worker", available: true,
                                     isDefault: false, reason: nil)
        try useStubGateway(list: [.hub, unflagged], pick: "qwen-local")
        XCTAssertFalse(EngineStore.usesQuickAsk("?weather"))
        XCTAssertTrue(EngineStore.isLocal("qwen-local"))
        XCTAssertFalse(EngineStore.isKnownCloud("qwen-local"))
        XCTAssertTrue(EngineStore.isKnownCloud("claude"))
    }

    func testRawLocalErrorsShownVerbatim() {
        XCTAssertEqual(EngineStore.errorText(Self.busy, prefix: "send failed: "), Self.busy)
        XCTAssertEqual(EngineStore.errorText(Self.wrongChat, prefix: "ask failed: "), Self.wrongChat)
        XCTAssertEqual(EngineStore.errorText("HTTP 500", prefix: "send failed: "), "send failed: HTTP 500")
    }

    /// Requirement 3: a down local pick blocks the chat send, and nothing reaches the gateway.
    func testDownLocalPickSendsNothing() async throws {
        try useStubGateway(list: [.hub, Self.localDown], pick: "qwen-local")
        XCTAssertNil(EngineStore.sendChoice().engine)
        XCTAssertNotNil(EngineStore.sendChoice().blocked)
        let model = ChatModel(sessionID: nil, title: "New chat")
        model.send("hello")
        try await settle(model)
        XCTAssertTrue(StubGateway.recorded().isEmpty)
        XCTAssertTrue(model.lines.last?.text.contains("Qwen 27B · Local only unavailable: gaming PC asleep") == true)
        XCTAssertEqual(UserDefaults.standard.string(forKey: "engineID"), "qwen-local")
        model.close()
        // positive control: the stub does intercept, so the zero above is real
        UserDefaults.standard.set("hub", forKey: "engineID")
        let hub = ChatModel(sessionID: nil, title: "New chat")
        hub.send("hello")
        try await settle(hub)
        XCTAssertEqual(StubGateway.recorded().map(\.path), ["/chat"])
        XCTAssertNil(StubGateway.recorded().first?.body["engine"])
        hub.close()
    }

    /// S1: the engine is fixed when the owner sends. A pick reset during the upload (another
    /// send succeeding) and a Retry after a failed upload both still name the local engine.
    func testLocalEngineFixedBeforeUpload() async throws {
        try useStubGateway(list: [.hub, Self.local], pick: "qwen-local")
        StubGateway.respond = { path, count in
            if path == "/central/attachments" {
                UserDefaults.standard.set("hub", forKey: "engineID")  // another send reset the pick
                return count == 1 ? (500, nil, nil) : (200, ["id": "f1"], nil)
            }
            return path == "/chat" ? (200, ["action": "answered", "reply": "ok"], nil) : (404, nil, nil)
        }
        let model = ChatModel(sessionID: nil, title: "New chat")
        model.addAttachment(ChatAttachment(name: "a.txt", mimeType: "text/plain", data: Data("x".utf8)))
        model.send("with a file")
        try await settle(model)
        XCTAssertTrue(model.canRetry)  // upload 1 failed, nothing posted to /chat
        XCTAssertEqual(StubGateway.recorded().map(\.path), ["/central/attachments"])
        model.retry()
        try await settle(model)
        let chat = StubGateway.recorded().filter { $0.path == "/chat" }
        XCTAssertEqual(chat.count, 1)
        XCTAssertEqual(chat.first?.body["engine"] as? String, "qwen-local")
        model.close()
    }

    /// S2: a local chat whose reply names a cloud engine fails closed: error line, no more sends.
    func testCloudReplyOnLocalChatBlocksSends() async throws {
        try useStubGateway(list: [.hub, Self.local], pick: "qwen-local")
        StubGateway.respond = { path, _ in
            path == "/chat" ? (200, ["action": "answered", "reply": "hi", "engine": "claude", "upstream": "mini-pc"], nil)
                : (404, nil, nil)
        }
        let model = ChatModel(sessionID: nil, title: "New chat")
        model.send("first")
        try await settle(model)
        XCTAssertEqual(model.lines.last?.role, .error)
        XCTAssertNil(model.localEngine)
        model.send("second")
        try await settle(model)
        XCTAssertEqual(StubGateway.recorded().count, 1)
        XCTAssertEqual(model.lines.last?.role, .error)
        model.close()
    }

    /// S3: a chat opened on a session with no engine posts nothing until the row is known;
    /// a local row then makes every follow-up name its engine and the chip local.
    func testChatOpenedWithoutEngineLooksUpRow() async throws {
        try useStubGateway(list: [.hub, Self.glm], pick: "hub")
        StubGateway.respond = { path, _ in
            if path == "/central/sessions" {
                return (200, [["session_id": "s9", "engine": "qwen-local", "local_only": true, "upstream": "gaming-pc"]], nil)
            }
            return path.hasSuffix("/message") ? (200, ["action": "delivered"], nil) : (404, nil, nil)
        }
        let model = ChatModel(sessionID: "s9", title: "Local chat")
        XCTAssertEqual(model.lockedLabel, "Checking engine…")
        model.send("too early")
        try await settle(model)
        XCTAssertTrue(StubGateway.recorded().isEmpty)
        await model.resolveEngine()
        XCTAssertNil(model.lockedLabel)
        XCTAssertEqual(model.localEngine, "qwen-local")
        XCTAssertEqual(model.lockedEngine, "qwen-local")
        model.retry()
        try await settle(model)
        XCTAssertEqual(StubGateway.recorded().map(\.path), ["/central/sessions/s9/message"])
        XCTAssertEqual(StubGateway.recorded().first?.body["engine"] as? String, "qwen-local")
        model.close()

        // a failed lookup keeps sends blocked
        StubGateway.respond = { _, _ in (500, nil, nil) }
        let unknown = ChatModel(sessionID: "s10", title: "Chat")
        await unknown.resolveEngine()
        XCTAssertTrue(unknown.lookupFailed)
        XCTAssertEqual(unknown.lockedLabel, "Engine unknown")
        unknown.send("hello")
        try await settle(unknown)
        XCTAssertEqual(StubGateway.recorded().count, 1)
        unknown.close()
    }

    /// R1: a lookup answer that lands after a New-session detach changes nothing.
    func testLateLookupAfterDetachIsIgnored() async throws {
        try useStubGateway(list: [.hub, Self.local], pick: "hub")
        StubGateway.respond = { path, _ in
            if path == "/central/sessions" {
                Thread.sleep(forTimeInterval: 0.4)  // still in flight when the owner detaches
                return (200, [["session_id": "s9", "engine": "qwen-local", "local_only": true]], nil)
            }
            return path == "/chat" ? (200, ["action": "started", "session_id": "h1", "engine": "claude", "reply": "ok"], nil)
                : (404, nil, nil)
        }
        let model = ChatModel(sessionID: "s9", title: "Local chat")
        let lookup = Task { await model.resolveEngine() }
        try await Task.sleep(nanoseconds: 100_000_000)
        model.newSession = true
        model.send("fresh chat")
        try await settle(model)
        await lookup.value
        XCTAssertEqual(model.sessionID, "h1")
        XCTAssertNil(model.localEngine)
        XCTAssertEqual(model.lockedEngine, "claude")
        XCTAssertFalse(model.lookupFailed)
        XCTAssertNil(StubGateway.recorded().first?.body["engine"])
        model.close()
    }

    /// R2: a refused send is never resent on a pick that moved since.
    func testRetryOfRefusedSendAfterPickChangePostsNothing() async throws {
        try useStubGateway(list: [.hub, Self.localDown], pick: "qwen-local")
        let model = ChatModel(sessionID: nil, title: "New chat")
        model.send("hello")
        try await settle(model)
        UserDefaults.standard.set("hub", forKey: "engineID")
        model.retry()
        try await settle(model)
        XCTAssertTrue(StubGateway.recorded().isEmpty)
        XCTAssertTrue(model.lines.last?.text.contains("The engine changed. Send again to use Hub.") == true)
        model.close()
    }

    /// R3: an explicit `local_only: false` means no "Local only" claim on screen; with the
    /// key missing, a "Local only" label still counts as local for sending.
    func testLocalClaimFollowsTheKey() throws {
        let explicitNo = #"[{"id":"q","label":"Qwen 27B","kind":"local","available":true,"default":false,"local_only":false}]"#
        let missing = #"[{"id":"q","label":"Qwen 27B · Local only","kind":"worker","available":true,"default":false}]"#
        let no = try XCTUnwrap(JSONDecoder().decode([EngineOption].self, from: Data(explicitNo.utf8)).first)
        XCTAssertFalse(no.claimsLocalOnly)
        XCTAssertEqual(no.displayLabel, "Qwen 27B")
        XCTAssertEqual(no.spokenLabel, "Qwen 27B")
        XCTAssertTrue(no.isLocalOption)  // sending stays fail closed
        let old = try XCTUnwrap(JSONDecoder().decode([EngineOption].self, from: Data(missing.utf8)).first)
        XCTAssertFalse(old.localOnly)
        XCTAssertTrue(old.claimsLocalOnly)
        // the cache keeps "no key" apart from false
        let cached = try JSONDecoder().decode([EngineOption].self, from: JSONEncoder().encode([old]))
        XCTAssertEqual(cached, [old])
        try useStubGateway(list: [.hub, old], pick: "q")
        XCTAssertTrue(EngineStore.isLocal("q"))
        XCTAssertFalse(EngineStore.usesQuickAsk("?weather"))
    }

    /// R4: a session the list does not have stays unsendable (no guess), says the way out,
    /// and New session then sends a fresh chat with the live pick.
    func testUnlistedSessionBlocksUntilNewSession() async throws {
        try useStubGateway(list: [.hub, Self.glm], pick: "glm")
        let savedDelay = ChatModel.lookupRecheckNanos
        ChatModel.lookupRecheckNanos = 10_000_000
        addTeardownBlock { ChatModel.lookupRecheckNanos = savedDelay }
        StubGateway.respond = { path, _ in
            if path == "/central/sessions" { return (200, [["session_id": "other"], ["bad": 1]], nil) }  // bad row skipped
            return path == "/chat" ? (200, ["action": "started", "session_id": "n1", "reply": "ok"], nil) : (404, nil, nil)
        }
        let model = ChatModel(sessionID: "gone", title: "Chat")
        await model.resolveEngine()
        XCTAssertTrue(model.lookupFailed)
        XCTAssertEqual(model.lockedLabel, "Engine unknown")
        XCTAssertEqual(model.lines.last?.text, ChatModel.notListedNote)
        model.send("hello")
        try await settle(model)
        XCTAssertTrue(StubGateway.recorded().isEmpty)
        model.newSession = true
        XCTAssertNil(model.lockedEngine)  // the picker is live again
        model.send("hello again")
        try await settle(model)
        XCTAssertEqual(StubGateway.recorded().map(\.path), ["/chat"])
        XCTAssertEqual(StubGateway.recorded().first?.body["engine"] as? String, "glm")
        XCTAssertNotEqual(StubGateway.recorded().first?.body["thread_id"] as? String, nil)
        model.close()
    }

    /// BINDING 1: the local engine id goes out on every message (first send, follow-ups
    /// on /chat and on the bound session, Retry), whatever the picker says afterwards.
    func testLocalChatNamesEngineOnEveryMessage() async throws {
        try useStubGateway(list: [.hub, Self.glm, Self.local], pick: "qwen-local")
        // reply 1 makes no session (follow-up stays on /chat); reply 2 binds one
        StubGateway.respond = { path, count in
            if path == "/chat" && count == 1 { return (200, ["action": "answered", "reply": "one", "upstream": "mini-pc"], nil) }
            if path == "/chat" { return (200, ["action": "started", "session_id": "s1", "reply": "two"], nil) }
            if path.hasSuffix("/message") { return (200, ["action": "delivered"], nil) }
            return (404, nil, nil)
        }
        let model = ChatModel(sessionID: nil, title: "New chat")
        model.send("first")
        try await settle(model)
        // the owner rule resets the picker after a good send; the chat must not follow it
        XCTAssertEqual(UserDefaults.standard.string(forKey: "engineID"), "hub")
        XCTAssertEqual(model.localEngine, "qwen-local")
        XCTAssertEqual(model.lines.last?.upstream, "mini-pc")
        model.send("second")
        try await settle(model)
        XCTAssertEqual(model.sessionID, "s1")
        XCTAssertEqual(model.sessionEngine, "qwen-local")  // locked chip never says Hub
        model.send("third")
        try await settle(model)
        let sent = StubGateway.recorded()
        XCTAssertEqual(sent.map(\.path), ["/chat", "/chat", "/central/sessions/s1/message"])
        XCTAssertEqual(sent.map { $0.body["engine"] as? String }, ["qwen-local", "qwen-local", "qwen-local"])
        model.close()

        // a chat opened on a local session row names its engine with no picker and no cached list
        UserDefaults.standard.removeObject(forKey: EngineStore.cacheKey)
        StubGateway.reset()
        let opened = ChatModel(sessionID: "s2", title: "Local chat", engine: "qwen-local", localOnly: true)
        opened.send("follow-up")
        try await settle(opened)
        XCTAssertEqual(StubGateway.recorded().map(\.path), ["/central/sessions/s2/message"])
        XCTAssertEqual(StubGateway.recorded().first?.body["engine"] as? String, "qwen-local")
        opened.close()
    }

    /// BINDING 2 + requirement 6: after a 400, 409, 503 or timeout the picker keeps the local
    /// pick, the router's wording shows verbatim, and Retry still names the local engine.
    func testFailedLocalSendKeepsPickAndRetriesLocal() async throws {
        // nil = any "send failed: …" line (the timeout wording is the system's)
        let cases: [(Int, String?, URLError?, String?)] = [
            (400, "bad request", nil, "send failed: bad request"),
            (409, Self.wrongChat, nil, Self.wrongChat),
            (503, Self.busy, nil, Self.busy),
            (0, nil, URLError(.timedOut), nil),
        ]
        for (status, detail, failure, shown) in cases {
            try useStubGateway(list: [.hub, Self.local], pick: "qwen-local")
            StubGateway.respond = { path, _ in
                path == "/chat" ? (status, detail.map { ["detail": $0] as [String: Any] }, failure) : (404, nil, nil)
            }
            let model = ChatModel(sessionID: nil, title: "New chat")
            model.send("hello")
            try await settle(model)
            XCTAssertEqual(UserDefaults.standard.string(forKey: "engineID"), "qwen-local", "status \(status)")
            if let shown = shown {
                XCTAssertEqual(model.lines.last?.text, shown)
            } else {
                XCTAssertTrue(model.lines.last?.text.hasPrefix("send failed: ") == true)
            }
            XCTAssertTrue(model.canRetry)
            // S5: "belongs to another engine" unlocks the picker; nothing is picked or resent for the owner
            XCTAssertEqual(model.lockedEngine, status == 409 ? nil : "qwen-local", "status \(status)")
            // even if the owner moves the picker, Retry of this local message stays local
            UserDefaults.standard.set("hub", forKey: "engineID")
            model.retry()
            try await settle(model)
            XCTAssertEqual(StubGateway.recorded().map { $0.body["engine"] as? String }, ["qwen-local", "qwen-local"],
                           "status \(status)")
            model.close()
        }
    }

    /// BINDING 3: while a local chat's send runs, a 2nd send is blocked (desktop `this.sending`).
    func testSecondSendBlockedWhileLocalSendRuns() async throws {
        try useStubGateway(list: [.hub, Self.local], pick: "qwen-local")
        StubGateway.respond = { path, _ in path == "/chat" ? (503, ["detail": Self.busy], nil) : (404, nil, nil) }
        let model = ChatModel(sessionID: nil, title: "New chat")
        model.send("one")
        XCTAssertTrue(model.isSending)
        model.send("two")
        try await settle(model)
        XCTAssertEqual(model.lines.filter { $0.role == .owner }.map(\.text), ["one"])
        XCTAssertEqual(StubGateway.recorded().count, 1)
        model.close()
    }

    // MARK: stub gateway

    /// Points the app at a stub host (only it is intercepted), seeds the cached engine
    /// list and the pick; everything is restored after the test.
    private func useStubGateway(list: [EngineOption], pick: String) throws {
        let defaults = UserDefaults.standard
        let saved = (defaults.object(forKey: "engineID"), defaults.object(forKey: EngineStore.cacheKey), AppSettings.gatewayURL)
        addTeardownBlock {
            URLProtocol.unregisterClass(StubGateway.self)
            StubGateway.reset()
            for (key, value) in [("engineID", saved.0), (EngineStore.cacheKey, saved.1)] {
                if let value = value { defaults.set(value, forKey: key) } else { defaults.removeObject(forKey: key) }
            }
            AppSettings.gatewayURL = saved.2
        }
        StubGateway.reset()
        URLProtocol.registerClass(StubGateway.self)
        AppSettings.gatewayURL = "https://\(StubGateway.host)"
        defaults.set(try JSONEncoder().encode(list), forKey: EngineStore.cacheKey)
        defaults.set(pick, forKey: "engineID")
    }

    /// Waits for the chat's send Task (main actor) to finish.
    private func settle(_ model: ChatModel) async throws {
        for _ in 0..<250 where model.isSending { try await Task.sleep(nanoseconds: 20_000_000) }
        XCTAssertFalse(model.isSending, "send still running")
    }

    // MARK: drawing

    /// The row, sized to its content at phone width.
    private func shot<V: View>(_ name: String, _ view: V) throws {
        let content = view.padding(16).frame(width: 390).background(Color.jarvisBackground)
        let probe = UIHostingController(rootView: content)
        let height = probe.sizeThatFits(in: CGSize(width: 390, height: 2000)).height
        // + room for the window's top safe area, which pushes the content down.
        try draw(name, AnyView(content.frame(maxHeight: .infinity, alignment: .top).background(Color.jarvisBackground)),
                 size: CGSize(width: 390, height: max(80, ceil(height)) + 120))
    }

    private func outputDir() throws -> URL {
        let env = ProcessInfo.processInfo.environment["CARD_SHOTS_DIR"]
        let dir = env.map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("card-shots")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func draw(_ name: String, _ view: AnyView, size: CGSize) throws {
        let host = UIHostingController(rootView: view.preferredColorScheme(.dark))
        host.overrideUserInterfaceStyle = .dark
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        let window = scene.map { UIWindow(windowScene: $0) } ?? UIWindow()
        window.frame = CGRect(origin: .zero, size: size)
        window.rootViewController = host
        window.isHidden = false
        host.view.frame = window.bounds
        host.view.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.8))  // let SwiftUI lay out
        let format = UIGraphicsImageRendererFormat()
        format.scale = 3
        let image = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        window.isHidden = true
        let png = try XCTUnwrap(image.pngData())
        try png.write(to: outputDir().appendingPathComponent("\(name).png"))
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

/// Answers requests to `host` only and records each one's path and JSON body.
final class StubGateway: URLProtocol {
    static let host = "gw.example.invalid"
    private static let lock = NSLock()
    private static var requests: [(method: String, path: String, body: [String: Any])] = []
    /// (path, how many requests that path has had) -> status + JSON body, or a transport error.
    static var respond: (String, Int) -> (Int, Any?, URLError?) = { _, _ in (404, nil, nil) }

    static func reset() {
        lock.lock(); defer { lock.unlock() }
        requests = []
        respond = { _, _ in (404, nil, nil) }
    }

    /// The POSTs (sends) only: SSE and any app poll (engines, sessions, notes) are GETs.
    static func recorded() -> [(path: String, body: [String: Any])] {
        lock.lock(); defer { lock.unlock() }
        return requests.filter { $0.method == "POST" }.map { ($0.path, $0.body) }
    }

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == host }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        let path = request.url?.path ?? ""
        let data = request.httpBody ?? request.httpBodyStream.map(Self.read)
        let body = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        Self.lock.lock()
        Self.requests.append((request.httpMethod ?? "GET", path, body))
        let count = Self.requests.filter { $0.path == path }.count
        let respond = Self.respond
        Self.lock.unlock()
        let (status, json, failure) = respond(path, count)
        if let failure = failure {
            client?.urlProtocol(self, didFailWithError: failure)
            return
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: json.flatMap { try? JSONSerialization.data(withJSONObject: $0) } ?? Data())
        client?.urlProtocolDidFinishLoading(self)
    }

    private static func read(_ stream: InputStream) -> Data {
        stream.open(); defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let n = stream.read(&buffer, maxLength: buffer.count)
            if n <= 0 { break }
            data.append(buffer, count: n)
        }
        return data
    }
}
