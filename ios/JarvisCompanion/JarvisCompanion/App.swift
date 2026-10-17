import AVFoundation
import Combine
import Speech
import SwiftUI
import UIKit
import UserNotifications

/// App entry + scene + WS lifecycle ownership: the AppModel glues the WebBridge
/// (gateway WS, role=ios) to the WebController (driven WKWebView), routes
/// gateway-pushed commands, and owns the ask flow (dictation + the shared
/// POST /siri/hey send path, Phase 0 spec behavior items 2-7).
@main
struct JarvisCompanionApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var model = AppModel()
    @UIApplicationDelegateAdaptor(OrientationDelegate.self) private var orientation

    init() {
        // BGTaskScheduler handlers must be registered before launch finishes
        // (J3): the refresh task is the WS/audio-keepalive fallback path.
        InboxNotifier.registerBackgroundRefresh(model: model)
    }

    var body: some Scene {
        WindowGroup {
            ContentView(model: model, dictation: model.dictation)
                .onOpenURL { url in model.openHandoff(url) }
                .onAppear { model.start() }
                .onChange(of: scenePhase) { model.sceneChanged($0) }
        }
    }
}

/// Home stays portrait; the pushed page (e.g. /screen, Mini PC full screen)
/// may rotate. Info.plist lists landscape so this mask is allowed to widen.
final class OrientationDelegate: NSObject, UIApplicationDelegate {
    static var allowLandscape = false

    func application(_ application: UIApplication,
                     supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        Self.allowLandscape ? .allButUpsideDown : .portrait
    }

    /// Re-query the mask now; back to Home also rotates to portrait.
    static func set(allowLandscape allow: Bool) {
        allowLandscape = allow
        for case let scene as UIWindowScene in UIApplication.shared.connectedScenes {
            scene.keyWindow?.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
            if !allow { scene.requestGeometryUpdate(.iOS(interfaceOrientations: .portrait)) }
        }
    }
}

/// Home tokens (Phase 0 spec "Tokens" section, copied verbatim): one accent,
/// flat surfaces, radius 16, no gradients, no glassmorphism. bg #0A0A0C,
/// surface #141418, text #F2F2F4, muted #8A8A93, accent #FF9F43, danger
/// #FF5C5C.
extension Color {
    static let jarvisBackground = Color(red: 10 / 255, green: 10 / 255, blue: 12 / 255) // #0A0A0C
    static let jarvisSurface = Color(red: 20 / 255, green: 20 / 255, blue: 24 / 255) // #141418
    static let jarvisText = Color(red: 242 / 255, green: 242 / 255, blue: 244 / 255) // #F2F2F4
    static let jarvisMuted = Color(red: 138 / 255, green: 138 / 255, blue: 147 / 255) // #8A8A93
    static let jarvisAccent = Color(red: 255 / 255, green: 159 / 255, blue: 67 / 255) // #FF9F43
    static let jarvisDanger = Color(red: 255 / 255, green: 92 / 255, blue: 92 / 255) // #FF5C5C
}

/// Radius token (same spec section): every rounded surface uses radius 16.
let jarvisRadius: CGFloat = 16

/// Motion line, and nothing else: light impact on press start, success
/// notification on reply, error notification on failure.
enum Haptics {
    static func light() {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    static func success() {
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    static func error() {
        UINotificationFeedbackGenerator().notificationOccurred(.error)
    }
}

/// Hold-to-talk dictation engine (Phase 0 spec, behavior items 2, 5, 7): a
/// SFSpeechAudioBufferRecognitionRequest fed by an AVAudioEngine input tap.
/// Locale.current first; nil or unavailable falls back to en-US.
/// requiresOnDeviceRecognition stays false (Arabic may need server
/// recognition). Main-thread confined: every callback hops to main before
/// touching published state.
final class DictationController: NSObject, ObservableObject {
    @Published var isListening = false
    /// Live partial transcript; the typed row's field mirrors this while held.
    @Published var partialTranscript = ""

    /// Fired on release (or the 60 s cap) with the final transcript. An empty
    /// string means nothing intelligible was captured: the caller shows
    /// "didn't catch that" and makes NO network request.
    var onFinalTranscript: ((String) -> Void)?
    /// Fired when mic or speech permission is denied/restricted.
    var onPermissionDenied: (() -> Void)?
    /// Fired when the recognizer is unavailable or the engine fails to start.
    var onEngineError: ((Error) -> Void)?

    private let recognizer: SFSpeechRecognizer?
    private let audioEngine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var bestTranscript = ""
    private var capTimer: Timer?
    private var finalizeWork: DispatchWorkItem?
    private var starting = false
    /// Set when release() lands while `starting` (async permission hops):
    /// pending start completions check this and abort to idle.
    private var releaseRequested = false
    /// Mirrors the physical hold (true from press to release/cancel) so the
    /// 60 s cap can tell a live hold from a gesture that was CANCELLED (F2).
    private var isHeld = false
    /// Set when either permission was .notDetermined at press time (F2): the
    /// permission alert cancels the gesture, so after the grant the start
    /// aborts to idle — the owner must press again.
    private var repressAfterGrant = false
    /// Set by cancelDiscard() so straggler recognition callbacks (the
    /// cancelled task's final error) don't deliver anything afterwards.
    private var discarding = false
    /// Per-hold generation: bumped on every press and when a hold's result
    /// is delivered. Recognizer callbacks and the cap timer capture it at
    /// start and are ignored once stale — so `task?.cancel()` re-firing the
    /// result closure during the 1.5 s grace wait cannot double-deliver.
    private var generation = 0

    override init() {
        if let current = SFSpeechRecognizer(locale: Locale.current), current.isAvailable {
            recognizer = current
        } else {
            recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        }
        super.init()
        // F2: an audio-session interruption (incoming call, Siri…) kills the
        // capture mid-hold; discard it rather than send a truncated take.
        NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: .main
        ) { [weak self] note in
            if (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt)
                == AVAudioSession.InterruptionType.began.rawValue {
                self?.cancelDiscard()
            }
        }
    }

    /// True between press start and the listening state actually engaging
    /// (covers the async permission ask), so the gesture cannot double-start.
    var isPressed: Bool { isListening || starting }

    /// Press start: request Speech Recognition + Microphone if not yet
    /// determined (behavior item 5), then start engine + recognition.
    func pressStarted() {
        guard !isPressed else { return }
        generation += 1
        releaseRequested = false
        discarding = false
        let speechStatus = SFSpeechRecognizer.authorizationStatus()
        if speechStatus == .denied || speechStatus == .restricted {
            onPermissionDenied?()
            return
        }
        starting = true
        isHeld = true
        repressAfterGrant = speechStatus == .notDetermined
            || AVAudioSession.sharedInstance().recordPermission == .undetermined
        if speechStatus == .notDetermined {
            SFSpeechRecognizer.requestAuthorization { [weak self] status in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    guard status == .authorized else {
                        self.starting = false
                        self.releaseRequested = false
                        self.onPermissionDenied?()
                        return
                    }
                    guard !self.releaseRequested else {
                        self.starting = false
                        self.releaseRequested = false
                        return
                    }
                    self.requestMicThenStart()
                }
            }
        } else {
            requestMicThenStart()
        }
    }

    private func requestMicThenStart() {
        AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
            DispatchQueue.main.async {
                guard let self = self else { return }
                guard granted else {
                    self.starting = false
                    self.releaseRequested = false
                    self.onPermissionDenied?()
                    return
                }
                guard !self.releaseRequested else {
                    self.starting = false
                    self.releaseRequested = false
                    return
                }
                if self.repressAfterGrant {
                    // F2: the permission alert cancelled the gesture — both
                    // permissions now granted, but return to idle without
                    // starting; the owner presses again.
                    self.starting = false
                    self.repressAfterGrant = false
                    return
                }
                self.startListening()
            }
        }
    }

    private func startListening() {
        defer { starting = false }
        guard !isListening else { return }
        guard let recognizer = recognizer, recognizer.isAvailable else {
            onEngineError?(NSError(
                domain: "dictation", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "speech recognition unavailable"]
            ))
            return
        }
        do {
            KeepAlive.beginListening()
            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            request.requiresOnDeviceRecognition = false
            self.request = request
            let input = audioEngine.inputNode
            let format = input.outputFormat(forBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
                request.append(buffer)
            }
            audioEngine.prepare()
            try audioEngine.start()
            bestTranscript = ""
            partialTranscript = ""
            let gen = generation
            task = recognizer.recognitionTask(with: request) { [weak self] result, error in
                DispatchQueue.main.async {
                    guard let self = self, self.generation == gen else { return }
                    self.handleResult(result, error)
                }
            }
            isListening = true
            // 60 s cap (behavior item 2): Apple's recognizer limit; auto-stop
            // and auto-send while still held, otherwise discard (F2: a
            // cancelled gesture must not auto-send unheld).
            capTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self = self, self.isListening, self.generation == gen else { return }
                    if self.isHeld {
                        self.release()
                    } else {
                        self.cancelDiscard()
                    }
                }
            }
        } catch {
            teardownAudio()
            onEngineError?(error)
        }
    }

    /// Shared stop sequence (release, isFinal, error and cancel paths):
    /// stop listening, kill the cap timer, tear the tap/engine down.
    private func stopCapture() {
        isListening = false
        capTimer?.invalidate()
        capTimer = nil
        teardownAudio()
    }

    /// Release: endAudio on the request, stop the tap/engine, restore the
    /// KeepAlive session, wait up to 1.5 s for isFinal, then send the best
    /// transcript (behavior item 2 / criterion 5).
    func release() {
        isHeld = false
        guard isListening else {
            // Release (or gesture CANCEL, F2) during the async start path:
            // flag it so the pending start aborts instead of running unheld.
            if starting { releaseRequested = true }
            return
        }
        stopCapture()
        let gen = generation
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.generation == gen else { return }
            self.finalizeWork = nil
            self.deliver()
        }
        finalizeWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: work)
    }

    /// F2: stop dictation and DISCARD the transcript (scene leaving active,
    /// audio-session interruption, 60 s cap with no hold). Same teardown as
    /// release() — tap removed, engine stopped, KeepAlive session restored —
    /// but nothing is sent.
    func cancelDiscard() {
        isHeld = false
        repressAfterGrant = false
        discarding = true
        // Abort any pending async start (same mechanism as a release while
        // starting); a fresh press resets the flag.
        releaseRequested = true
        finalizeWork?.cancel()
        finalizeWork = nil
        if isListening {
            stopCapture()
        }
        starting = false
        task?.cancel()
        task = nil
        bestTranscript = ""
        partialTranscript = ""
    }

    private func teardownAudio() {
        audioEngine.inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        request?.endAudio()
        request = nil
        KeepAlive.endListening()
    }

    private func handleResult(_ result: SFSpeechRecognitionResult?, _ error: Error?) {
        if discarding { return }
        if let result = result {
            bestTranscript = result.bestTranscription.formattedString
            partialTranscript = bestTranscript
            if result.isFinal {
                // Final arrived (possibly during the post-release wait):
                // deliver immediately instead of riding out the 1.5 s.
                finalizeWork?.cancel()
                finalizeWork = nil
                task = nil
                if isListening {
                    stopCapture()
                }
                deliver()
            }
            return
        }
        if error != nil {
            // Recognizer died mid-hold: stop and deliver best effort with
            // whatever transcript was captured so far.
            finalizeWork?.cancel()
            finalizeWork = nil
            if isListening {
                stopCapture()
            }
            deliver()
        }
    }

    private func deliver() {
        // Invalidate this hold's stragglers BEFORE cancelling, so the result
        // closure `task?.cancel()` re-fires into is dropped (no double send).
        generation += 1
        task?.cancel()
        task = nil
        let text = bestTranscript.trimmingCharacters(in: .whitespacesAndNewlines)
        bestTranscript = ""
        partialTranscript = ""
        onFinalTranscript?(text)
    }
}

/// Glue owning the WS lifecycle (WebBridge), the driven web view (WebController)
/// and the ask flow (DictationController + the shared /siri/hey send path).
/// Gateway-pushed commands (`navigate`, `evaluateJS`, `screenshot`) act on the
/// web view; results go back over WS as `{id, type: "result", payload:
/// {ok, data}}`.
final class AppModel: ObservableObject {
    let bridge = WebBridge()
    let web = WebController()
    let dictation = DictationController()
    /// Single InboxModel instance: the Home unread badge and the Inbox sheet
    /// share this state.
    let inbox = InboxModel()
    /// J3: retained so UNUserNotificationCenter keeps it alive (an unretained
    /// delegate gets its taps dropped); routes notification taps into
    /// openHandoff, the same path as the jarvis:// url scheme.
    private let notificationDelegate = InboxNotificationDelegate()

    @Published var statusText = "starting"
    @Published var gatewayURLField = AppSettings.gatewayURL {
        didSet { AppSettings.gatewayURL = gatewayURLField.trimmingCharacters(in: .whitespaces) }
    }
    @Published var tokenField = AppSettings.token {
        didSet { AppSettings.token = tokenField }
    }

    // Ask-flow state (Phase 0 spec "Home" section).
    @Published var typedText = ""
    /// Owner's last sent transcript, shown muted above the reply.
    @Published var ownerText: String?
    @Published var replyText: String?
    @Published var replyIsError = false
    /// Muted `route · ms` tag under the reply — quick-ask only; the plain
    /// brain path keeps it nil (spec §22).
    @Published var routeTag: String?
    @Published var isSending = false
    @Published var showSettings = false
    @Published var showPushedPage = false
    @Published var showInbox = false
    /// Session the last /siri/hey answer pointed at ("Open chat" on Home).
    @Published var lastSession: ChatTarget?
    /// Chat sheet presented from Home.
    @Published var openChat: ChatTarget?
    /// Bumped when the fullScreenCover dismisses so Home's hidden web host
    /// re-claims the shared WKWebView (see JarvisWebView).
    @Published var webHostToken = 0

    private var cancellables: Set<AnyCancellable> = []

    /// Same guard WebBridge.connect() uses (WebBridge.swift, empty-config row,
    /// behavior item 6): the ask path is independent of the WS, but it still
    /// needs both values configured.
    var askReady: Bool {
        !gatewayURLField.trimmingCharacters(in: .whitespaces).isEmpty && !tokenField.isEmpty
    }

    init() {
        bridge.$connected
            .combineLatest(bridge.$lastError)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] connected, error in
                self?.statusText = connected ? "connected" : (error ?? "disconnected")
            }
            .store(in: &cancellables)

        // Inbox state is a separate ObservableObject: cascade its changes so
        // views observing AppModel (the Home unread badge) repaint with it.
        inbox.objectWillChange
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.objectWillChange.send() }
            .store(in: &cancellables)

        dictation.onFinalTranscript = { [weak self] text in
            guard let self = self else { return }
            if text.isEmpty {
                // Empty/whitespace transcript: NO network request (criterion 5).
                self.setFailure("didn't catch that")
            } else {
                self.ask(text)
            }
        }
        dictation.onPermissionDenied = { [weak self] in
            self?.setFailure("mic or speech permission denied, enable in Settings")
        }
        dictation.onEngineError = { [weak self] error in
            self?.fail(error)
        }
    }

    func start() {
        bridge.onCommand = { [weak self] id, type, payload in
            self?.handleCommand(id: id, type: type, payload: payload)
        }
        // The audio session must start foregrounded; it then holds the WS in
        // the background (KeepAlive).
        KeepAlive.start()
        KeepAlive.requestNotificationPermission()
        // J3: inbox notification taps funnel through openHandoff, exactly like
        // a jarvis://inbox link arriving.
        notificationDelegate.onOpenInbox = { [weak self] in
            if let url = URL(string: "jarvis://inbox") { self?.openHandoff(url) }
        }
        // Owner-note taps open that session's chat (Home already shows the note).
        notificationDelegate.onOpenNote = { [weak self] sid, title in
            if !sid.isEmpty { self?.openChat = ChatTarget(sessionID: sid, title: title) }
        }
        UNUserNotificationCenter.current().delegate = notificationDelegate
        bridge.connect()
        // Prime the unread badge even before the sheet is ever opened.
        inbox.refresh()
    }

    /// Shared send path (behavior item 4): mic release, the typed row, and the
    /// three quick chips all funnel here. POST <gateway>/siri/hey with the ios
    /// token as bearer, 30 s timeout, `reply` into the conversation area,
    /// field cleared on success, `ask failed: <error>` on failure.
    func ask(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending, askReady else { return }
        ownerText = text
        replyText = nil
        routeTag = nil
        lastSession = nil
        replyIsError = false
        isSending = true
        // F3: URLComponents-built endpoint — https enforced, no query or
        // fragment leaking into the path.
        guard let url = Self.heyEndpoint() else {
            isSending = false
            fail("gateway URL must be https")
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 30
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.isSending = false
                if let error = error {
                    self.fail(error)
                    return
                }
                // F1: surface the real HTTP status instead of letting a 401
                // show up as "malformed reply".
                if let http = response as? HTTPURLResponse,
                   !(200...299).contains(http.statusCode) {
                    if http.statusCode == 401 || http.statusCode == 403 {
                        self.fail("unauthorized, check the token in Settings")
                    } else {
                        self.fail("HTTP \(http.statusCode)")
                    }
                    return
                }
                guard let data = data,
                      let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      let reply = obj["reply"] as? String else {
                    self.fail(NSError(
                        domain: "ask", code: 3,
                        userInfo: [NSLocalizedDescriptionKey: "malformed reply"]
                    ))
                    return
                }
                self.replyText = reply
                if let sid = obj["session_id"] as? String, !sid.isEmpty {
                    self.lastSession = ChatTarget(sessionID: sid, title: obj["title"] as? String ?? "Chat")
                }
                self.replyIsError = false
                self.typedText = ""
                Haptics.success()
            }
        }.resume()
    }

    /// F3: <gateway>/siri/hey from the saved setting via URLComponents —
    /// scheme must be https, the existing path is kept with no double slash,
    /// any query/fragment is dropped. Every ask entry point (chips, typed
    /// send, mic release) funnels through ask(), so all share this helper.
    private static func heyEndpoint() -> URL? {
        gatewayEndpoint("/siri/hey")
    }

    /// Shared F3 builder behind heyEndpoint(): same https enforcement and
    /// path appending, suffix picked per endpoint (/siri/hey, /ask, /pc/wake).
    static func gatewayEndpoint(_ suffix: String) -> URL? {
        guard var comps = URLComponents(
            string: AppSettings.gatewayURL.trimmingCharacters(in: .whitespaces)
        ), comps.scheme?.lowercased() == "https", comps.host != nil else {
            return nil
        }
        var path = comps.path
        while path.hasSuffix("/") { path.removeLast() }
        comps.path = path + suffix
        comps.query = nil
        comps.fragment = nil
        return comps.url
    }

    /// Typed-row dispatcher (spec §22): `?…` goes to the terse quick-ask
    /// pipeline, anything else to the full brain via ask().
    func sendTyped() {
        let text = typedText.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("?") {
            quickAsk(text)
        } else {
            ask(text)
        }
    }

    /// Quick-ask (spec §22): POST <gateway>/ask {"q": <text minus ?>,
    /// "brain": "auto"}; decodes {answer, route, ms, source}. A non-null
    /// answer lands in the reply area with a `route · ms` tag; a null answer
    /// (skip short-circuit / routed to the brain) shows the reroute hint.
    /// Auth and failure surfaces reuse the ask() pattern verbatim.
    func quickAsk(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let question = String(text.dropFirst()).trimmingCharacters(in: .whitespaces)
        // F1: a bare `?` (question empty after strip) never sends, mirroring
        // ask()'s `!text.isEmpty` guard.
        guard text.hasPrefix("?"), !question.isEmpty, !isSending, askReady else { return }
        ownerText = text
        replyText = nil
        routeTag = nil
        lastSession = nil
        replyIsError = false
        isSending = true
        guard let url = Self.gatewayEndpoint("/ask") else {
            isSending = false
            fail("gateway URL must be https")
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 45
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["q": question, "brain": "auto"])
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.isSending = false
                if let error = error {
                    self.fail(error)
                    return
                }
                if let http = response as? HTTPURLResponse,
                   !(200...299).contains(http.statusCode) {
                    if http.statusCode == 401 || http.statusCode == 403 {
                        self.fail("unauthorized, check the token in Settings")
                    } else {
                        self.fail("HTTP \(http.statusCode)")
                    }
                    return
                }
                guard let data = data,
                      let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      obj["answer"] != nil,
                      let route = obj["route"] as? String,
                      let ms = obj["ms"] as? Int else {
                    self.fail(NSError(
                        domain: "ask", code: 3,
                        userInfo: [NSLocalizedDescriptionKey: "malformed reply"]
                    ))
                    return
                }
                if let answer = obj["answer"] as? String {
                    self.replyText = answer
                    self.routeTag = "\(route) · \(ms) ms"
                } else {
                    self.replyText = "routed to the full brain — ask without the ? or use the mic"
                }
                self.replyIsError = false
                self.typedText = ""
                Haptics.success()
            }
        }.resume()
    }

    /// Wake the PC (spec §23): POST <gateway>/pc/wake (no body). The reply
    /// area shows "PC: waking…" immediately, then one of the outcome strings;
    /// a 400 (no MAC configured) surfaces the endpoint's detail verbatim.
    /// Timeout is 100 s — the gateway polls up to 90 s for the agent.
    func wakePC() {
        guard !isSending, askReady else { return }
        ownerText = nil
        replyText = "PC: waking…"
        routeTag = nil
        lastSession = nil
        replyIsError = false
        isSending = true
        guard let url = Self.gatewayEndpoint("/pc/wake") else {
            isSending = false
            fail("gateway URL must be https")
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 100
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.isSending = false
                if let error = error {
                    self.fail(error)
                    return
                }
                if let http = response as? HTTPURLResponse,
                   !(200...299).contains(http.statusCode) {
                    if http.statusCode == 400,
                       let data = data,
                       let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                       let detail = obj["detail"] as? String {
                        self.setFailure(detail)
                    } else if http.statusCode == 401 || http.statusCode == 403 {
                        self.fail("unauthorized, check the token in Settings")
                    } else {
                        self.fail("HTTP \(http.statusCode)")
                    }
                    return
                }
                guard let data = data,
                      let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      let online = obj["online"] as? Bool,
                      let waited = obj["waited_ms"] as? Int else {
                    self.fail(NSError(
                        domain: "ask", code: 3,
                        userInfo: [NSLocalizedDescriptionKey: "malformed reply"]
                    ))
                    return
                }
                if online {
                    self.replyText = "PC online in \(Int((Double(waited) / 1000).rounded())) s"
                    self.replyIsError = false
                    Haptics.success()
                } else {
                    self.setFailure("PC not seen after 90 s")
                }
            }
        }.resume()
    }

    /// Shared failure surface: every failure path (HTTP/URL/parse errors,
    /// permission denial, engine errors, empty transcript) funnels here so
    /// the error haptic always fires; only one place sets the reply state.
    private func setFailure(_ message: String) {
        replyText = message
        replyIsError = true
        Haptics.error()
    }

    private func fail(_ message: String) {
        setFailure("ask failed: \(message)")
    }

    private func fail(_ error: Error) {
        fail(error.localizedDescription)
    }

    /// DESIGN.md "Feature data flows" #2: a `jarvis://handoff/<id>` link
    /// arrival opens the app, which sends `handoff_open` over WS; the gateway
    /// replies `{id, ok, data: {url}}` and pushes `navigate` — the WKWebView
    /// loads the checkout URL and the Mac drives it from there.
    func openHandoff(_ url: URL) {
        // jarvis://inbox opens the vendor-mail inbox sheet.
        if url.scheme == "jarvis", url.host == "inbox" {
            showInbox = true
            inbox.refresh()
            statusText = "inbox"
            return
        }
        guard url.scheme == "jarvis", url.host == "handoff",
              let id = url.pathComponents.dropFirst().first, !id.isEmpty else {
            statusText = "unrecognized jarvis:// link: \(url.absoluteString)"
            return
        }
        statusText = "handoff \(id): opening"
        bridge.send(type: "handoff_open", payload: ["id": id]) { [weak self] ok, data in
            guard let self = self else { return }
            if ok, let urlString = data["url"] as? String, let target = URL(string: urlString) {
                self.web.navigate(to: target)
                self.showPushedPage = true
                self.statusText = "handoff \(id): driving"
            } else {
                self.statusText = "handoff \(id): gateway declined (\(data))"
            }
        }
    }

    func sceneChanged(_ phase: ScenePhase) {
        // DESIGN.md "Failure handling": JS is unreliable in background, so the
        // app only acts while foregrounded.
        web.isForeground = phase == .active
        // F2: leaving the active scene cancels the hold — discard the
        // capture instead of sending it.
        if phase != .active { dictation.cancelDiscard() }
        // J3: leaving the foreground arms the BGAppRefresh fallback for when
        // the audio-keepalive WS gets reclaimed; submitting while foregrounded
        // would be pointless (the live WS already delivers inbox_new).
        if phase == .background { InboxNotifier.scheduleBackgroundRefresh() }
    }

    private func handleCommand(id: String, type: String, payload: [String: Any]) {
        switch type {
        case "navigate":
            guard let urlString = payload["url"] as? String, let url = URL(string: urlString) else {
                reportResult(id: id, ok: false, data: ["error": "navigate: missing url"])
                return
            }
            if !web.isForeground { KeepAlive.notifyHandoff(id: id) }
            web.navigate(to: url)
            // A push (re-)presents the driven page over Home.
            showPushedPage = true
            reportResult(id: id, ok: true, data: ["url": urlString])
        case "evaluateJS":
            // Covers the T5.2 credential-prefill JS: the gateway injects the
            // prefill script as a `js` payload whose `expected_host` (HIGH-3)
            // binds it to the sign-in origin — the WebController holds it until
            // that host finishes loading and runs it in the .defaultClient
            // world; any other host drops it with a bare "host mismatch" data.
            guard let js = payload["js"] as? String else {
                reportResult(id: id, ok: false, data: ["error": "evaluateJS: missing js"])
                return
            }
            let expectedHost = payload["expected_host"] as? String
            web.evaluateJS(js, expectedHost: expectedHost) { [weak self] result in
                switch result {
                case .success(let value):
                    self?.reportResult(id: id, ok: true, data: ["value": "\(value)"])
                case .failure(let error):
                    if case JarvisError.hostMismatch = error {
                        self?.reportResult(id: id, ok: false, data: ["error": "host mismatch"])
                    } else {
                        self?.reportResult(id: id, ok: false, data: ["error": error.localizedDescription])
                    }
                }
            }
        case "screenshot":
            web.screenshot { [weak self] result in
                switch result {
                case .success(let base64):
                    self?.reportResult(id: id, ok: true, data: ["image": base64])
                case .failure(let error):
                    self?.reportResult(id: id, ok: false, data: ["error": error.localizedDescription])
                }
            }
        case "inbox_new":
            // Gateway push from POST /inbox/notify: refresh the shared
            // InboxModel so the badge and an open sheet update. handleCommand
            // runs on main (WebBridge hops before dispatch), matching ask().
            // J3: local notification ONLY while backgrounded (same guard as
            // the navigate case) — foreground, the user is looking at the
            // badge/sheet already; iOS conventions say don't spam on-screen.
            inbox.refresh()
            if !web.isForeground {
                InboxNotifier.notify(
                    unread: payload["unread"] as? Int ?? inbox.unread,
                    new: payload["new"] as? Int ?? 0
                )
            }
            reportResult(id: id, ok: true, data: ["unread": payload["unread"] ?? 0])
        case "owner_note":
            // Gateway push (gateway/notify.py): a late answer to a Home
            // question, or a session waiting for the owner. Home shows it like
            // a reply; a notification goes out either way.
            let title = payload["title"] as? String ?? "Jarvis"
            let text = payload["text"] as? String ?? ""
            let sid = (payload["session_id"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            ownerText = title
            replyText = text
            replyIsError = false
            routeTag = nil
            lastSession = sid.map { ChatTarget(sessionID: $0, title: title) }
            InboxNotifier.notifyNote(title: title, body: text, sessionID: sid)
            reportResult(id: id, ok: true, data: [:])
        default:
            reportResult(id: id, ok: false, data: ["error": "unknown command: \(type)"])
        }
    }

    private func reportResult(id: String, ok: Bool, data: [String: Any]) {
        bridge.send(type: "result", payload: ["id": id, "ok": ok, "data": data])
    }
}

/// Home screen (Phase 0 product spec "UI" section): dark only. JARVIS
/// wordmark + status pill + gear top, conversation area middle, quick chips
/// above the 88 pt hero hold-to-talk mic, slim typed row below it.
struct ContentView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var dictation: DictationController

    @ScaledMetric(relativeTo: .body) private var replySize: CGFloat = 21
    @ScaledMetric(relativeTo: .caption) private var markSize: CGFloat = 13
    /// F2: press tracking — SwiftUI resets this to false when the gesture
    /// ends OR is CANCELLED (system alert under the finger, incoming call,
    /// backgrounding), so a cancel runs the exact same path as a release.
    @GestureState private var micHeld = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Ask-chip focus target (spec §23): tapping `Ask` puts `?` in the typed
    /// row and raises the keyboard on it.
    @FocusState private var typedFieldFocused: Bool
    /// Wordmark menu choice: Controls (this screen), Sessions, Floor.
    @AppStorage("homeView") private var homeView: HomeView = .controls

    var body: some View {
        ZStack {
            Color.jarvisBackground.ignoresSafeArea()
            VStack(spacing: 24) {
                topBar
                switch homeView {
                case .controls:
                    conversation
                    chips
                    micSection
                    typedRow
                case .sessions:
                    SessionsBoard()
                case .floor:
                    FloorView { model.openChat = $0 }
                }
            }
            .padding(16)
        }
        .preferredColorScheme(.dark)
        // Keeps the shared WKWebView on-window while no push is presented,
        // so off-cover screenshot/evaluateJS commands still run against a
        // windowed view (see JarvisWebView).
        .background { hiddenWebHost }
        .sheet(isPresented: $model.showSettings) { SettingsSheet(model: model) }
        .sheet(isPresented: $model.showInbox) { InboxScreen(model: model.inbox) }
        .sheet(item: $model.openChat) { ChatView(target: $0) }
        .fullScreenCover(isPresented: $model.showPushedPage) {
            PushedPageView(model: model, web: model.web)
        }
    }

    private var topBar: some View {
        HStack(spacing: 16) {
            Menu {
                Picker("View", selection: $homeView) {
                    ForEach(HomeView.allCases, id: \.self) { Text($0.label).tag($0) }
                }
            } label: {
                HStack(spacing: 6) {
                    Text("JARVIS")
                        .font(.system(size: markSize, weight: .semibold))
                        .tracking(2.6)
                    Image(systemName: "chevron.down")
                        .font(.system(size: markSize * 0.8, weight: .semibold))
                }
                .foregroundColor(.jarvisMuted)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .accessibilityLabel("Home view: \(homeView.label)")
            Spacer(minLength: 8)
            statusPill
            Button {
                model.showInbox = true
            } label: {
                ZStack(alignment: .topTrailing) {
                    Image(systemName: "tray")
                        .font(.system(size: 17, weight: .medium))
                        .foregroundColor(.jarvisMuted)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                    if model.inbox.unread > 0 {
                        Text("\(model.inbox.unread)")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundColor(.white)
                            .padding(.horizontal, 5)
                            .frame(minWidth: 16, minHeight: 16)
                            .background(Capsule().fill(Color.jarvisDanger))
                            .offset(x: -6, y: 6)
                    }
                }
            }
            .accessibilityLabel("Inbox")
            Button {
                model.showSettings = true
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 17, weight: .medium))
                    .foregroundColor(.jarvisMuted)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("Settings")
        }
    }

    private var statusPill: some View {
        HStack(spacing: 6) {
            Circle().fill(pillDot).frame(width: 7, height: 7)
            Text(pillText)
                .font(.footnote)
                .foregroundColor(.jarvisMuted)
                .lineLimit(1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Capsule().fill(Color.jarvisSurface))
    }

    /// Pill copy: connected / disconnected / short error (anything longer,
    /// e.g. WebBridge's "connect failed: ..." or the empty-config hint,
    /// truncates).
    private var pillText: String {
        switch model.statusText {
        case "connected", "disconnected", "starting":
            return model.statusText
        default:
            let text = model.statusText
            return text.count > 18 ? String(text.prefix(18)) + "…" : text
        }
    }

    /// Dot color derives from bridge state, not the free-text pill copy:
    /// accent while connected, danger on the last error, muted otherwise —
    /// so a `handoff …: opening/driving` status never shows red.
    private var pillDot: Color {
        if model.bridge.connected { return .jarvisAccent }
        if model.bridge.lastError != nil { return .jarvisDanger }
        return .jarvisMuted
    }

    private var conversation: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if let owner = model.ownerText {
                    Text(owner)
                        .font(.body)
                        .foregroundColor(.jarvisMuted)
                        .textSelection(.enabled)
                }
                if let reply = model.replyText {
                    Text(reply)
                        .font(.system(size: replySize))
                        .foregroundColor(model.replyIsError ? .jarvisDanger : .jarvisText)
                        .textSelection(.enabled)
                        .transition(.opacity)
                    if let tag = model.routeTag {
                        Text(tag)
                            .font(.footnote)
                            .foregroundColor(.jarvisMuted)
                            .textSelection(.enabled)
                    }
                    if let session = model.lastSession {
                        Button {
                            model.openChat = session
                        } label: {
                            Text("Open chat")
                                .font(.subheadline.weight(.medium))
                                .foregroundColor(.jarvisAccent)
                                .padding(.horizontal, 16)
                                .frame(minHeight: 44)
                                .background(
                                    RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisAccent.opacity(0.14))
                                )
                        }
                    }
                } else if model.ownerText == nil {
                    Text("Hold the mic. Ask for your Mac or PC screen.")
                        .font(.system(size: replySize))
                        .foregroundColor(.jarvisMuted)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 8)
        }
        // Reply fade-in, 200 ms (Motion line).
        .animation(.easeOut(duration: 0.2), value: model.replyText)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    /// Chips, two rows (spec §23): screen chips Mac / PC / Mini PC on top,
    /// Status + `Ask` (focus the typed row, `?` prefilled) + `Wake PC` below.
    private var chips: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                ForEach([
                    Chip(label: "Mac", payload: "Show Mac screen"),
                    Chip(label: "PC", payload: "Show PC screen"),
                    Chip(label: "Mini PC", payload: "Show Mini PC screen"),
                ]) { chip in
                    Button {
                        model.ask(chip.payload)
                    } label: {
                        chipLabel(chip.label)
                    }
                    .disabled(chipDisabled)
                    .opacity(chipDisabled ? 0.35 : 1.0)
                }
            }
            HStack(spacing: 8) {
                Button {
                    model.ask("Status")
                } label: {
                    chipLabel("Status")
                }
                .disabled(chipDisabled)
                .opacity(chipDisabled ? 0.35 : 1.0)
                Button {
                    model.typedText = "?"
                    typedFieldFocused = true
                } label: {
                    chipLabel("Ask")
                }
                .disabled(chipDisabled)
                .opacity(chipDisabled ? 0.35 : 1.0)
                Button {
                    model.wakePC()
                } label: {
                    chipLabel("Wake PC")
                }
                .disabled(chipDisabled)
                .opacity(chipDisabled ? 0.35 : 1.0)
            }
        }
    }

    private var chipDisabled: Bool {
        !model.askReady || model.isSending || dictation.isListening
    }

    private func chipLabel(_ text: String) -> some View {
        Text(text)
            .font(.subheadline)
            .foregroundColor(.jarvisText)
            .lineLimit(1)
            .minimumScaleFactor(0.85)
            .padding(.horizontal, 16)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
    }

    private var micSection: some View {
        VStack(spacing: 8) {
            ZStack {
                if dictation.isListening { PulsingRing() }
                micButton
            }
            .frame(height: 96)
            if !model.askReady {
                Text("set gateway in settings")
                    .font(.footnote)
                    .foregroundColor(.jarvisMuted)
            }
        }
    }

    private var micButton: some View {
        Circle()
            .fill(Color.jarvisAccent)
            .frame(width: 88, height: 88)
            .scaleEffect(dictation.isListening && !reduceMotion ? 1.06 : 1.0)
            .animation(.easeInOut(duration: 0.2), value: dictation.isListening)
            .overlay(
                Image(systemName: "mic.fill")
                    .font(.system(size: 34, weight: .medium))
                    .foregroundColor(.white)
            )
            .opacity(model.askReady ? 1.0 : 0.35)
            .contentShape(Circle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        guard model.askReady, !model.isSending else { return }
                        if !dictation.isPressed {
                            Haptics.light()
                            dictation.pressStarted()
                        }
                    }
                    .updating($micHeld) { _, state, _ in state = true }
            )
            // F2: fires on both a normal lift and a CANCELLED gesture (the
            // @GestureState auto-reset); release() aborts a pending start or
            // stops + sends a live one.
            .onChange(of: micHeld) { held in
                if !held { dictation.release() }
            }
            .accessibilityLabel("Hold to talk")
            .accessibilityAddTraits(.isButton)
    }

    private var typedRow: some View {
        HStack(spacing: 8) {
            TextField(
                "or type…",
                text: typedBinding,
                prompt: Text("or type…").foregroundColor(.jarvisMuted)
            )
                .font(.body)
                .foregroundColor(.jarvisText)
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
                .keyboardType(.default)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.sentences)
                .submitLabel(.send)
                .focused($typedFieldFocused)
                .onSubmit {
                    // Manual send only while idle: the recognizer owns the
                    // field during a hold (criterion 7). `?…` dispatches to
                    // quick-ask (spec §22), plain text to the full brain.
                    if !dictation.isListening { model.sendTyped() }
                }
            sendButton
        }
    }

    /// While held, the field mirrors the live partial transcript (behavior
    /// item 2); typing (mis-heard fixups, behavior item 3) only lands when
    /// idle so it cannot fight the recognizer updates.
    private var typedBinding: Binding<String> {
        Binding(
            get: {
                dictation.isListening ? dictation.partialTranscript : model.typedText
            },
            set: { newValue in
                if !dictation.isListening { model.typedText = newValue }
            }
        )
    }

    private var sendButton: some View {
        Button {
            model.sendTyped()
        } label: {
            Group {
                if model.isSending {
                    ProgressView().tint(.jarvisAccent)
                } else {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 30))
                        .foregroundColor(.jarvisAccent)
                }
            }
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
        }
        .disabled(!model.askReady || model.isSending || dictation.isListening)
        .opacity(model.askReady && !model.isSending && !dictation.isListening ? 1.0 : 0.35)
        .accessibilityLabel("Send")
    }

    private var hiddenWebHost: some View {
        JarvisWebView(controller: model.web)
            .opacity(0.01)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
            .id(model.webHostToken)
    }
}

/// Quick chip (Home "quick chips"): short display label, full command payload.
private struct Chip: Identifiable {
    let label: String
    let payload: String
    var id: String { payload }
}

/// Listening ring (Motion line): 1.2 s ease pulse in accent while held.
/// Reduce Motion renders it static (no repeating animation).
private struct PulsingRing: View {
    @State private var pulse = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Circle()
            .stroke(Color.jarvisAccent, lineWidth: 2)
            .frame(width: 88, height: 88)
            .scaleEffect(pulse ? 1.3 : 1.0)
            .opacity(pulse ? 0.1 : 0.6)
            .onAppear { if !reduceMotion { pulse = true } }
            .animation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true), value: pulse)
    }
}

/// Settings sheet (Phase 0 spec "Settings sheet"): the existing gateway URL
/// field, iOS token SecureField, Reconnect button and live status line moved
/// off the main screen (same AppModel/AppSettings storage), plus a
/// version/build footer.
struct SettingsSheet: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Settings")
                    .font(.headline)
                    .foregroundColor(.jarvisText)
                Spacer()
                Button("Done") { dismiss() }
                    .font(.body.weight(.medium))
                    .foregroundColor(.jarvisAccent)
                    .frame(minHeight: 44)
            }
            TextField(
                "Gateway URL (https://…:8790)",
                text: $model.gatewayURLField,
                prompt: Text("Gateway URL (https://…:8790)").foregroundColor(.jarvisMuted)
            )
                .font(.body)
                .foregroundColor(.jarvisText)
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            SecureField(
                "iOS token",
                text: $model.tokenField,
                prompt: Text("iOS token").foregroundColor(.jarvisMuted)
            )
                .font(.body)
                .foregroundColor(.jarvisText)
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
            Button {
                model.bridge.disconnect()
                model.bridge.connect()
            } label: {
                Text("Reconnect")
                    .font(.body.weight(.medium))
                    .foregroundColor(.jarvisAccent)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(
                        RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisAccent.opacity(0.14))
                    )
            }
            Text(model.statusText)
                .font(.footnote)
                .foregroundColor(.jarvisMuted)
            Spacer()
            Text("version \(versionString) (build \(buildString))")
                .font(.footnote)
                .foregroundColor(.jarvisMuted)
        }
        .padding(16)
        .background(Color.jarvisBackground.ignoresSafeArea())
        .preferredColorScheme(.dark)
    }

    private var versionString: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    }

    private var buildString: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?"
    }
}

/// Gateway-pushed page (Phase 0 spec "Pushed page"): the driven WKWebView
/// presented as a fullScreenCover with a slim top bar; Done closes it and
/// returns Home. Status/reconnect logic is unchanged (it lives in AppModel /
/// WebBridge, not here).
struct PushedPageView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var web: WebController
    @Environment(\.verticalSizeClass) private var verticalSizeClass

    /// Page asked for full screen AND the phone is landscape (compact height):
    /// portrait always shows Done, whatever the page posted.
    private var fullscreen: Bool { web.screenFullscreen && verticalSizeClass == .compact }

    var body: some View {
        VStack(spacing: 0) {
            if !fullscreen {
                HStack {
                    Button {
                        model.showPushedPage = false
                    } label: {
                        Text("Done")
                            .font(.body.weight(.medium))
                            .foregroundColor(.jarvisAccent)
                    }
                    .frame(minHeight: 44)
                    Spacer()
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 4)
            }
            JarvisWebView(controller: model.web)
        }
        .background(Color.jarvisBackground.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .statusBarHidden(fullscreen)
        .onAppear { OrientationDelegate.set(allowLandscape: true) }
        .onDisappear {
            OrientationDelegate.set(allowLandscape: false)
            // Bump so Home's hidden host re-claims the shared WKWebView now
            // that the cover host is gone.
            model.webHostToken += 1
        }
    }
}
