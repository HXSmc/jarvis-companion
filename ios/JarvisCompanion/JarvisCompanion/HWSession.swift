import Foundation

/// What the brain wants drawn on the live picture. `rect` is normalized 0...1 in
/// the image's own coordinates (origin top-left), so it maps onto any view size.
struct HWOverlay: Equatable {
    var rect: CGRect
    var action: String
    var dir: CGVector
    var label: String
    var frameId: Int?
}

/// A session-card request to open Hardware Mode for a Claude session.
struct HWTarget: Identifiable {
    let id: String  // Claude session id
    let task: String
}

/// Hardware Mode link to the gateway's `/hwmode/ws`: frames up, spoken
/// guidance + overlay down. The brain (a lean Claude Code session) runs on the
/// mini PC; this class only moves messages and keeps the latest answer.
@MainActor
final class HWSession: ObservableObject {
    @Published var connected = false
    @Published var status = "Connecting…"
    @Published var say = ""
    @Published var step = 0
    @Published var total = 0
    @Published var overlay: HWOverlay?
    @Published var thinking = false
    @Published var lastSeconds: Double?

    /// Called with each spoken line (early `say`, or the reply's when no early one came) that the
    /// phone voices itself: Arabic, no server voice, or the server audio did not come in time.
    var onSay: ((String) -> Void)?
    /// Jarvis's own voice for a line, from the gateway: 16-bit mono PCM, sample rate, the line.
    var onAudio: ((Data, Double, String) -> Void)?
    /// The gateway voices English lines itself (`ready` names the voice).
    private(set) var serverVoice = false
    /// How long a line waits for its server audio before the phone's own voice reads it.
    var audioWait: TimeInterval = 6  // seen live: audio 2.3 s after a 4 s line; long lines take longer
    var onOverlay: ((HWOverlay?) -> Void)?
    /// The brain session is up: send a frame and the first question.
    var onReady: (() -> Void)?

    private var socket: URLSessionWebSocketTask?
    private var urlSession: URLSession?
    private var taskText = ""
    private var nextFrameId = 1
    private var saidThisTurn = false
    private var quietTurn = false
    private var locateTurn = false
    /// The line waiting for its server audio, and audio that came before its line did.
    private var awaitingAudio: String?
    private var earlyAudio: [String: (Data, Double)] = [:]

    /// False when the gateway URL is unusable (nothing was started).
    @discardableResult
    func connect(task: String) -> Bool {
        taskText = task
        guard let https = AppModel.gatewayEndpoint("/hwmode/ws"),
              var comps = URLComponents(url: https, resolvingAgainstBaseURL: false) else {
            status = "Set the gateway URL (https) in Settings"
            return false
        }
        comps.scheme = "wss"
        guard let url = comps.url else { return false }
        disconnect()
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
        send(["type": "start", "task": task])
        return true
    }

    func disconnect() {
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        urlSession?.invalidateAndCancel()
        urlSession = nil
        connected = false
        thinking = false
    }

    /// Sends a camera frame; returns its id so a later overlay can be matched to it.
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
        guard !trimmed.isEmpty, connected else { return }
        thinking = true
        saidThisTurn = false
        quietTurn = quiet
        locateTurn = locate
        send(["type": "ask", "text": trimmed, "frame": withFrame])
    }

    /// The tracker lost the part: ask where `label` is in the newest frame. The answer only
    /// moves the box (it speaks only if the part is out of view).
    func locate(_ label: String) {
        ask("[locate] I lost the highlight. Point again at the same part (\(label)) in this picture. "
            + "If you see it, reply with its overlay and \"say\": \"\". If it is not in view, set overlay to null and say where to aim.",
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
        case "say":
            if let text = m["text"] as? String { speak(text) }
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
        case "reply":
            thinking = false
            lastSeconds = (m["total_s"] as? NSNumber)?.doubleValue
            guard let d = m["data"] as? [String: Any] else {
                if !quietTurn { speak("Sorry, I didn't get that. Ask me again.") }  // a background check stays silent
                return
            }
            let said = ((d["say"] as? String) ?? "").trimmingCharacters(in: .whitespaces)
            let parsed = Self.parseOverlay(d["overlay"], width: (m["w"] as? NSNumber)?.doubleValue,
                                           height: (m["h"] as? NSNumber)?.doubleValue,
                                           frameId: (m["frame_id"] as? NSNumber)?.intValue)
            if locateTurn {  // only the box moves; speak only when the part is out of view
                if !saidThisTurn, !said.isEmpty { speak(said) }
                if parsed != nil || !said.isEmpty {
                    overlay = parsed
                    onOverlay?(overlay)
                }
                return
            }
            if quietTurn && said.isEmpty { return }  // watch check: all good, keep the current guidance
            if !saidThisTurn, !said.isEmpty { speak(said) }
            // The gateway gates the step (done, or seen done): show what it allows.
            step = (d["step"] as? NSNumber)?.intValue ?? step
            total = (d["of"] as? NSNumber)?.intValue ?? total
            overlay = parsed
            onOverlay?(overlay)
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

    /// `target_px` is in pixels of the image the brain saw (w×h); normalize and clamp.
    static func parseOverlay(_ raw: Any?, width: Double?, height: Double?, frameId: Int?) -> HWOverlay? {
        guard let o = raw as? [String: Any],
              let px = (o["target_px"] as? [NSNumber])?.map({ $0.doubleValue }), px.count == 4,
              let w = width, let h = height, w > 0, h > 0 else { return nil }
        let x1 = min(max(min(px[0], px[2]) / w, 0), 1), x2 = min(max(max(px[0], px[2]) / w, 0), 1)
        let y1 = min(max(min(px[1], px[3]) / h, 0), 1), y2 = min(max(max(px[1], px[3]) / h, 0), 1)
        guard x2 > x1, y2 > y1 else { return nil }
        let d = (o["arrow_dir"] as? [NSNumber])?.map { CGFloat($0.doubleValue) } ?? [0, 0]
        return HWOverlay(rect: CGRect(x: x1, y: y1, width: x2 - x1, height: y2 - y1),
                         action: (o["action"] as? String) ?? "look",
                         dir: d.count == 2 ? CGVector(dx: d[0], dy: d[1]) : .zero,
                         label: (o["label"] as? String) ?? "",
                         frameId: frameId)
    }
}
