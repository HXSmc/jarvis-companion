import AVFoundation
import Combine
import Speech
import SwiftUI
import UIKit

/// App entry + scene + WS lifecycle ownership: the AppModel glues the WebBridge
/// (gateway WS, role=ios) to the WebController (driven WKWebView), routes
/// gateway-pushed commands, and owns the ask flow (dictation + the shared
/// POST /siri/hey send path, Phase 0 spec behavior items 2-7).
@main
struct JarvisCompanionApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model, dictation: model.dictation)
                .onOpenURL { url in model.openHandoff(url) }
                .onAppear { model.start() }
                .onChange(of: scenePhase) { model.sceneChanged($0) }
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

    override init() {
        if let current = SFSpeechRecognizer(locale: Locale.current), current.isAvailable {
            recognizer = current
        } else {
            recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        }
        super.init()
    }

    /// True between press start and the listening state actually engaging
    /// (covers the async permission ask), so the gesture cannot double-start.
    var isPressed: Bool { isListening || starting }

    /// Press start: request Speech Recognition + Microphone if not yet
    /// determined (behavior item 5), then start engine + recognition.
    func pressStarted() {
        guard !isPressed else { return }
        let speechStatus = SFSpeechRecognizer.authorizationStatus()
        if speechStatus == .denied || speechStatus == .restricted {
            onPermissionDenied?()
            return
        }
        starting = true
        if speechStatus == .notDetermined {
            SFSpeechRecognizer.requestAuthorization { [weak self] status in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    guard status == .authorized else {
                        self.starting = false
                        self.onPermissionDenied?()
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
                    self.onPermissionDenied?()
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
                request.appendAudioPCMBuffer(buffer)
            }
            audioEngine.prepare()
            try audioEngine.start()
            bestTranscript = ""
            partialTranscript = ""
            task = recognizer.recognitionTask(with: request) { [weak self] result, error in
                DispatchQueue.main.async { self?.handleResult(result, error) }
            }
            isListening = true
            // 60 s cap (behavior item 2): Apple's recognizer limit; auto-stop
            // and auto-send rather than stranding the hold.
            capTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in
                DispatchQueue.main.async { self?.release() }
            }
        } catch {
            teardownAudio()
            onEngineError?(error)
        }
    }

    /// Release: endAudio on the request, stop the tap/engine, restore the
    /// KeepAlive session, wait up to 1.5 s for isFinal, then send the best
    /// transcript (behavior item 2 / criterion 5).
    func release() {
        guard isListening else { return }
        isListening = false
        capTimer?.invalidate()
        capTimer = nil
        teardownAudio()
        let work = DispatchWorkItem { [weak self] in
            self?.finalizeWork = nil
            self?.deliver()
        }
        finalizeWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: work)
    }

    private func teardownAudio() {
        audioEngine.inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        request?.endAudio()
        request = nil
        KeepAlive.endListening()
    }

    private func handleResult(_ result: SFSpeechRecognitionResult?, _ error: Error?) {
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
                    isListening = false
                    capTimer?.invalidate()
                    capTimer = nil
                    teardownAudio()
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
                isListening = false
                capTimer?.invalidate()
                capTimer = nil
                teardownAudio()
            }
            deliver()
        }
    }

    private func deliver() {
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
    @Published var isSending = false
    @Published var showSettings = false
    @Published var showPushedPage = false
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

        dictation.onFinalTranscript = { [weak self] text in
            guard let self = self else { return }
            if text.isEmpty {
                // Empty/whitespace transcript: NO network request (criterion 5).
                self.replyText = "didn't catch that"
                self.replyIsError = true
            } else {
                self.ask(text)
            }
        }
        dictation.onPermissionDenied = { [weak self] in
            self?.replyText = "mic or speech permission denied, enable in Settings"
            self?.replyIsError = true
        }
        dictation.onEngineError = { [weak self] error in
            self?.replyText = "ask failed: \(error.localizedDescription)"
            self?.replyIsError = true
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
        bridge.connect()
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
        replyIsError = false
        isSending = true
        var base = AppSettings.gatewayURL.trimmingCharacters(in: .whitespaces)
        while base.hasSuffix("/") { base.removeLast() }
        guard let url = URL(string: base + "/siri/hey") else {
            isSending = false
            fail(NSError(
                domain: "ask", code: 2,
                userInfo: [NSLocalizedDescriptionKey: "invalid gateway URL"]
            ))
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 30
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.isSending = false
                if let error = error {
                    self.fail(error)
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
                self.replyIsError = false
                self.typedText = ""
                Haptics.success()
            }
        }.resume()
    }

    private func fail(_ error: Error) {
        replyText = "ask failed: \(error.localizedDescription)"
        replyIsError = true
        Haptics.error()
    }

    /// DESIGN.md "Feature data flows" #2: a `jarvis://handoff/<id>` link
    /// arrival opens the app, which sends `handoff_open` over WS; the gateway
    /// replies `{id, ok, data: {url}}` and pushes `navigate` — the WKWebView
    /// loads the checkout URL and the Mac drives it from there.
    func openHandoff(_ url: URL) {
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

    var body: some View {
        ZStack {
            Color.jarvisBackground.ignoresSafeArea()
            VStack(spacing: 24) {
                topBar
                conversation
                chips
                micSection
                typedRow
            }
            .padding(16)
        }
        .preferredColorScheme(.dark)
        // Keeps the shared WKWebView on-window while no push is presented,
        // so off-cover screenshot/evaluateJS commands still run against a
        // windowed view (see JarvisWebView).
        .background(hiddenWebHost)
        .sheet(isPresented: $model.showSettings) { SettingsSheet(model: model) }
        .fullScreenCover(isPresented: $model.showPushedPage) {
            PushedPageView(model: model)
        }
    }

    private var topBar: some View {
        HStack(spacing: 16) {
            Text("JARVIS")
                .font(.system(size: markSize, weight: .semibold))
                .tracking(2.6)
                .foregroundColor(.jarvisMuted)
            Spacer(minLength: 8)
            statusPill
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

    private var pillDot: Color {
        switch model.statusText {
        case "connected": return .jarvisAccent
        case "disconnected", "starting": return .jarvisMuted
        default: return .jarvisDanger
        }
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

    private var chips: some View {
        HStack(spacing: 8) {
            ForEach(["Show Mac screen", "Show PC screen", "Status"], id: \.self) { title in
                Button {
                    model.ask(title)
                } label: {
                    Text(title)
                        .font(.subheadline)
                        .foregroundColor(.jarvisText)
                        .padding(.horizontal, 16)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(RoundedRectangle(cornerRadius: 16).fill(Color.jarvisSurface))
                }
                .disabled(!model.askReady || model.isSending)
            }
        }
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
            .scaleEffect(dictation.isListening ? 1.06 : 1.0)
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
                    .onEnded { _ in dictation.release() }
            )
            .accessibilityLabel("Hold to talk")
            .accessibilityAddTraits(.isButton)
    }

    private var typedRow: some View {
        HStack(spacing: 8) {
            TextField("or type…", text: typedBinding)
                .font(.body)
                .foregroundColor(.jarvisText)
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: 16).fill(Color.jarvisSurface))
                .keyboardType(.default)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.sentences)
                .submitLabel(.send)
                .onSubmit { model.ask(model.typedText) }
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
            model.ask(model.typedText)
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
        .disabled(!model.askReady || model.isSending)
        .accessibilityLabel("Send")
    }

    private var hiddenWebHost: some View {
        JarvisWebView(controller: model.web)
            .frame(width: 1, height: 1)
            .opacity(0.01)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
            .id(model.webHostToken)
    }
}

/// Listening ring (Motion line): 1.2 s ease pulse in accent while held.
private struct PulsingRing: View {
    @State private var pulse = false

    var body: some View {
        Circle()
            .stroke(Color.jarvisAccent, lineWidth: 2)
            .frame(width: 88, height: 88)
            .scaleEffect(pulse ? 1.3 : 1.0)
            .opacity(pulse ? 0.1 : 0.6)
            .onAppear { pulse = true }
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
            TextField("Gateway URL (https://…:8790)", text: $model.gatewayURLField)
                .font(.body)
                .foregroundColor(.jarvisText)
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: 16).fill(Color.jarvisSurface))
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            SecureField("iOS token", text: $model.tokenField)
                .font(.body)
                .foregroundColor(.jarvisText)
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: 16).fill(Color.jarvisSurface))
            Button {
                model.bridge.disconnect()
                model.bridge.connect()
            } label: {
                Text("Reconnect")
                    .font(.body.weight(.medium))
                    .foregroundColor(.jarvisAccent)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(
                        RoundedRectangle(cornerRadius: 16).fill(Color.jarvisAccent.opacity(0.14))
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

    var body: some View {
        VStack(spacing: 0) {
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
            JarvisWebView(controller: model.web)
        }
        .background(Color.jarvisBackground.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .onDisappear {
            // Bump so Home's hidden host re-claims the shared WKWebView now
            // that the cover host is gone.
            model.webHostToken += 1
        }
    }
}
