import Foundation

/// What the brain wants drawn on the live picture: ONE simple outline shape.
/// `rect` is normalized 0...1 in the image's own coordinates (origin top-left),
/// so it maps onto any view size.
struct HWShape: Equatable {
    var kind: String  // "rect" | "circle" | "triangle"
    var rect: CGRect
    var label: String
    var frameId: Int?
}

/// The one status the screen may show, most urgent first: camera, connection,
/// research, thinking, speaking, listening.
enum HWPhase: Equatable {
    case noCamera, connecting, offline, researching, thinking, speaking, listening

    /// `connecting`: the socket is up but the gateway has not said "ready" yet (not a failure).
    static func current(cameraAuthorized: Bool, connected: Bool, researching: Bool,
                        thinking: Bool, speaking: Bool, listening: Bool, connecting: Bool = false) -> HWPhase {
        if !cameraAuthorized { return .noCamera }
        if !connected { return connecting ? .connecting : .offline }
        if researching { return .researching }
        if thinking { return .thinking }
        if speaking { return .speaking }
        return .listening
    }

    var label: String {
        switch self {
        case .noCamera: return "Camera is off: allow it in Settings"
        case .connecting: return "Connecting…"
        case .offline: return "Can't reach Jarvis · tap to retry"
        case .researching: return "Researching…"
        case .thinking: return "Thinking…"
        case .speaking: return "Speaking"
        case .listening: return "Listening"
        }
    }
}

/// A session-card request to open Hardware Mode for a Claude session.
struct HWTarget: Identifiable {
    let id: String  // Claude session id
    let task: String
}

/// Hardware Mode link to the gateway's `/hwmode/ws`: frames up, spoken
/// guidance + one outline shape down. The brain (a lean Claude Code session)
/// runs on the mini PC; this class only moves messages and keeps the latest answer.
@MainActor
final class HWSession: ObservableObject {
    @Published var connected = false
    @Published var status = "Connecting…"
    @Published var say = ""
    @Published var shape: HWShape?
    @Published var thinking = false
    @Published var lastSeconds: Double?
    /// A research turn is running on the gateway ("Find out about X").
    @Published var researching = false
    /// Explanation cards over the camera (research answers). Replaced by each reply
    /// that carries any; only the Clear button empties them.
    @Published var cards: [HUDCard] = []

    /// Called with each spoken line (early `say`, or the reply's when no early one came) that the
    /// phone voices itself: Arabic, no server voice, or the server audio did not come in time.
    var onSay: ((String) -> Void)?
    /// An instant cached ack clip ("Checking.", ...): play at once; it must not change
    /// `say` or touch the audio the line that follows is still waiting for.
    var onAck: ((Data, Double, String) -> Void)?
    /// Jarvis's own voice for a line, from the gateway: 16-bit mono PCM, sample rate, the line.
    var onAudio: ((Data, Double, String) -> Void)?
    /// The gateway voices English lines itself (`ready` names the voice).
    private(set) var serverVoice = false
    /// How long a line waits for its server audio before the phone's own voice reads it.
    var audioWait: TimeInterval = 6  // seen live: audio 2.3 s after a 4 s line; long lines take longer
    var onShape: ((HWShape?) -> Void)?
    /// The brain session is up. A preloaded task asks at once; voice-first waits for words.
    var onReady: (() -> Void)?
    /// A Claude session was started from research findings: show its title.
    var onSession: ((String) -> Void)?

    private var socket: URLSessionWebSocketTask?
    private var urlSession: URLSession?
    private var nextFrameId = 1
    private var saidThisTurn = false
    private var quietTurn = false
    private var locateTurn = false
    /// The line waiting for its server audio, and audio that came before its line did.
    private var awaitingAudio: String?
    private var earlyAudio: [String: (Data, Double)] = [:]
    /// Words spoken before the gateway said "ready" (voice-first: the owner talks at once).
    private var pendingAsk: (text: String, withFrame: Bool)?

    /// False when the gateway URL is unusable (nothing was started).
    @discardableResult
    func connect(task: String, sessionId: String? = nil) -> Bool {
        guard let https = AppModel.gatewayEndpoint("/hwmode/ws"),
              var comps = URLComponents(url: https, resolvingAgainstBaseURL: false) else {
            status = "Set the gateway URL (https) in Settings"
            return false
        }
        comps.scheme = "wss"
        guard let url = comps.url else { return false }
        disconnect()
        pendingAsk = nil
        status = "Connecting…"
        var request = URLRequest(url: url)
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .default)
        urlSession = session
        let ws = session.webSocketTask(with: request)
        ws.maximumMessageSize = 8 * 1024 * 1024
        socket = ws
        ws.resume()
        receive(on: ws)
        var start: [String: Any] = ["type": "start", "task": task]
        if let sessionId { start["session_id"] = sessionId }  // the gateway reports results back to it
        send(start)
        return true
    }

    func disconnect() {
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        urlSession?.invalidateAndCancel()
        urlSession = nil
        connected = false
        thinking = false
        researching = false
    }

    /// The phone is closing: the gateway posts the report back to the session itself.
    func end() {
        send(["type": "end"])
    }

    /// The first ask after `ready`: a preloaded task starts at once; voice-first
    /// sends nothing, because the owner's first words become the task.
    static func firstAsk(initialTask: String) -> String? {
        initialTask.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : "Let's start."
    }

    /// Words spoken before "ready" pile into ONE ask, joined with a space.
    /// The frame flag sticks once set: any sentence may name what the camera sees.
    static func joinedPending(_ old: (text: String, withFrame: Bool)?, _ new: String,
                              _ withFrame: Bool) -> (text: String, withFrame: Bool) {
        old.map { (text: "\($0.text) \(new)", withFrame: $0.withFrame || withFrame) }
            ?? (text: new, withFrame: withFrame)
    }

    /// Sends a camera frame; returns its id so a later shape can be matched to it.
    @discardableResult
    func sendFrame(_ jpeg: Data) -> Int {
        let id = nextFrameId
        nextFrameId += 1
        send(["type": "frame", "id": id, "jpeg": jpeg.base64EncodedString()])
        return id
    }

    /// `quiet`: a watch-mode check; an empty answer changes nothing on screen.
    func ask(_ text: String, withFrame: Bool = true, quiet: Bool = false, locate: Bool = false) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        guard connected else {
            // The first words are the task: keep them until "ready", joined, not overwritten.
            if socket != nil, !quiet, !locate { pendingAsk = Self.joinedPending(pendingAsk, trimmed, withFrame) }
            return
        }
        thinking = true
        saidThisTurn = false
        quietTurn = quiet
        locateTurn = locate
        send(["type": "ask", "text": trimmed, "frame": withFrame])
    }

    /// The tracker lost the part: ask where `label` is in the newest frame. The answer only
    /// moves the shape (it speaks only if the part is out of view).
    func locate(_ label: String) {
        ask("[locate] I lost the highlight. Point again at the same part (\(label)) in this picture. "
            + "If you see it, reply with its shape and \"say\": \"\". If it is not in view, set shape to null and say where to aim.",
            quiet: true, locate: true)
    }

    /// Phone-side voice state, logged by the gateway (nothing comes back).
    func sendDiag(_ data: [String: Any]) {
        send(["type": "diag", "data": data])
    }

    private func send(_ object: [String: Any]) {
        guard let socket, let data = try? JSONSerialization.data(withJSONObject: object),
              let text = String(data: data, encoding: .utf8) else { return }
        socket.send(.string(text)) { _ in }
    }

    private func receive(on ws: URLSessionWebSocketTask) {
        ws.receive { @Sendable [weak self] result in
            Task { @MainActor in
                guard let self, self.socket === ws else { return }
                switch result {
                case .failure(let error):
                    self.connected = false
                    self.thinking = false
                    self.status = "Disconnected: \(error.localizedDescription)"
                case .success(let message):
                    var data: Data?
                    switch message {
                    case .string(let s): data = s.data(using: .utf8)
                    case .data(let d): data = d
                    @unknown default: data = nil
                    }
                    if let data, let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        self.handle(object)
                    }
                    self.receive(on: ws)
                }
            }
        }
    }

    /// One gateway message. Internal so tests can replay scripted sessions.
    func handle(_ m: [String: Any]) {
        switch m["type"] as? String {
        case "ready":
            connected = true
            status = "Live"
            serverVoice = m["voice"] is String
            onReady?()
            if let queued = pendingAsk {
                pendingAsk = nil
                ask(queued.text, withFrame: queued.withFrame)
            }
        case "say":
            if let text = m["text"] as? String { speak(text) }
        case "ack":
            // Instant clip: straight to the speaker, past everything else.
            guard let text = m["text"] as? String, let b64 = m["pcm"] as? String,
                  let pcm = Data(base64Encoded: b64) else { return }
            onAck?(pcm, (m["rate"] as? NSNumber)?.doubleValue ?? 24000, text)
        case "audio":
            guard let text = m["text"] as? String, let b64 = m["pcm"] as? String,
                  let pcm = Data(base64Encoded: b64) else { return }
            let rate = (m["rate"] as? NSNumber)?.doubleValue ?? 24000
            if awaitingAudio == text {
                awaitingAudio = nil
                onAudio?(pcm, rate, text)
            } else {
                earlyAudio = [text: (pcm, rate)]  // its line is still coming; a late one (fallback spoke) is dropped
            }
        case "research":
            researching = (m["state"] as? String) == "started"
        case "aside":
            // A spoken aside (a research summary): heard, never shown; it must not
            // touch `say`, the instruction card or the shape.
            guard let text = m["text"] as? String,
                  !text.trimmingCharacters(in: .whitespaces).isEmpty else { return }
            if let b64 = m["pcm"] as? String, let pcm = Data(base64Encoded: b64) {
                onAck?(pcm, (m["rate"] as? NSNumber)?.doubleValue ?? 24000, text)
            } else {
                onSay?(text)  // no server audio: the phone's own voice reads it
            }
        case "session":
            if let title = m["title"] as? String { onSession?(title) }
        case "reply":
            thinking = false
            lastSeconds = (m["total_s"] as? NSNumber)?.doubleValue
            guard let d = m["data"] as? [String: Any] else {
                if !quietTurn { speak("Sorry, I didn't get that. Ask me again.") }  // a background check stays silent
                return
            }
            let said = ((d["say"] as? String) ?? "").trimmingCharacters(in: .whitespaces)
            let parsed = Self.parseShape(d["shape"], width: (m["w"] as? NSNumber)?.doubleValue,
                                         height: (m["h"] as? NSNumber)?.doubleValue,
                                         frameId: (m["frame_id"] as? NSNumber)?.intValue)
            let newCards = HUDCard.list(from: d["cards"])
            if (d["research"] as? Bool) == true {
                // A research turn explains on the side: only the cards change; the
                // instruction, the shape and the tracker stay exactly as they were.
                if !newCards.isEmpty { cards = newCards }
                return
            }
            if !newCards.isEmpty { cards = newCards }  // only the Clear button empties them
            if locateTurn {  // only the shape moves; speak only when the part is out of view
                if !saidThisTurn, !said.isEmpty { speak(said) }
                if parsed != nil || !said.isEmpty {
                    shape = parsed
                    onShape?(shape)
                }
                return
            }
            if quietTurn && said.isEmpty { return }  // watch check: all good, keep the current guidance
            if !saidThisTurn, !said.isEmpty { speak(said) }
            shape = parsed
            onShape?(shape)
        case "error":
            let error = (m["error"] as? String) ?? "error"
            guard error != "busy" else { return }  // the answer already in flight still arrives
            thinking = false
            status = error
        default:
            break
        }
    }

    private func speak(_ text: String) {
        guard !text.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        saidThisTurn = true
        say = text
        guard serverVoice, !Self.isArabic(text) else {
            awaitingAudio = nil
            onSay?(text)
            return
        }
        if case let (pcm, rate)? = earlyAudio.removeValue(forKey: text) {
            awaitingAudio = nil
            onAudio?(pcm, rate, text)
            return
        }
        awaitingAudio = text
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64((self?.audioWait ?? 0) * 1_000_000_000))
            guard let self, self.awaitingAudio == text else { return }
            self.awaitingAudio = nil  // the server voice failed or is slow: the phone reads it
            self.onSay?(text)
        }
    }

    /// Same range as the gateway's check: Arabic lines are never voiced there.
    static func isArabic(_ text: String) -> Bool {
        text.unicodeScalars.contains { (0x0600...0x06FF).contains($0.value) }
    }

    /// `box` is in pixels of the image the brain saw (w×h); normalize and clamp.
    /// Unknown kinds are dropped: only plain outline shapes are drawn.
    static func parseShape(_ raw: Any?, width: Double?, height: Double?, frameId: Int?) -> HWShape? {
        guard let o = raw as? [String: Any],
              let kind = o["kind"] as? String, ["rect", "circle", "triangle"].contains(kind),
              let px = (o["box"] as? [NSNumber])?.map({ $0.doubleValue }), px.count == 4,
              let w = width, let h = height, w > 0, h > 0 else { return nil }
        let x1 = min(max(min(px[0], px[2]) / w, 0), 1), x2 = min(max(max(px[0], px[2]) / w, 0), 1)
        let y1 = min(max(min(px[1], px[3]) / h, 0), 1), y2 = min(max(max(px[1], px[3]) / h, 0), 1)
        guard x2 > x1, y2 > y1 else { return nil }
        return HWShape(kind: kind, rect: CGRect(x: x1, y: y1, width: x2 - x1, height: y2 - y1),
                       label: (o["label"] as? String) ?? "", frameId: frameId)
    }
}
