import SwiftUI

/// Hardware Mode: point the phone at the hardware, talk to Jarvis, and follow
/// the instruction drawn on the live picture (one outline shape that stays on
/// the part as the phone moves). Voice-first: the camera, the connection and
/// the microphone all start on open; the first words become the task.
struct HardwareModeView: View {
    var initialTask: String = ""
    /// Set when opened from a session card: the gateway reports results back to it.
    var sessionId: String? = nil

    @Environment(\.dismiss) private var dismiss
    @StateObject private var camera = HWCamera()
    @StateObject private var hw = HWSession()
    @StateObject private var voice = HWVoice()
    @State private var tracker: HWTracker?
    @State private var tracked: CGRect?
    @State private var lost = false
    @State private var task = ""
    @State private var typed = ""
    @State private var showKeyboard = false
    @State private var torch = false
    @State private var lastHeard = ""
    @State private var lastChange: Date?
    @State private var lastWatch = Date.distantPast
    @State private var ticks = 0
    @State private var lastLocate = Date.distantPast
    @State private var wasConnected = false
    @State private var spokeOffline = false
    @State private var sessionPill: String?
    @State private var sessionPillId: UUID?
    /// The last few JPEGs sent, by frame id: the tracker starts on the one the brain answered about.
    @State private var sentFrames: [Int: Data] = [:]
    @FocusState private var typing: Bool
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            HWCameraPreview(session: camera.session).ignoresSafeArea()
            // Measured full-screen, like the preview, so shapes land on the part.
            GeometryReader { geo in
                // The brain's shape always shows; tracking only keeps it on the part as the phone moves.
                if let shape = hw.shape {
                    // Lost: dimmed at the last known spot while the brain is asked to find it again.
                    HWShapeLayer(shape: shape, rect: viewRect(tracked ?? shape.rect, in: geo.size))
                        .opacity(lost ? 0.35 : 1)
                        .allowsHitTesting(false)
                }
                // Research cards around the part, never over it. HUDLayer keeps its own taps.
                HUDLayer(cards: hw.cards, avoid: hw.shape.map { viewRect(tracked ?? $0.rect, in: geo.size) })
            }
            .ignoresSafeArea()
            VStack(spacing: 10) {
                topBar
                instructionCard
                Spacer()
                if lost && hw.shape != nil {
                    pill("Finding the part again…", color: .orange)
                }
                if let title = sessionPill {
                    pill("Session started: \(title)", color: .blue)
                }
                if let err = voice.error { pill(err, color: .red) }
                bottomBar
            }
            .padding(16)
        }
        .onAppear(perform: setUp)
        .onDisappear(perform: tearDown)
        .onReceive(ticker) { _ in
            voice.ensureRunning()  // the audio engine can stop on its own; bring it back
            // Background frames: only when the picture changed. No model call.
            guard hw.connected else { return }
            ticks += 1
            if ticks == 3 || ticks % 20 == 0 { hw.sendDiag(voice.diagnostics()) }
            if let jpeg = camera.jpeg(onlyIfChanged: true) {
                send(jpeg)
                lastChange = Date()
            } else {
                watchIfSettled()
            }
            relocateIfLost()
        }
        .onChange(of: hw.connected) { ok in
            if ok {
                wasConnected = true
                spokeOffline = false
            } else if wasConnected, !spokeOffline {  // one spoken nudge per drop, not per refresh
                spokeOffline = true
                voice.speak("I can't reach the gateway. Tap retry.")
            }
        }
        .onChange(of: voice.heard) { heard in
            // Prefetch: ship a fresh frame the moment speech starts.
            if hw.connected, !heard.isEmpty, heard.split(separator: " ").count == 1, let jpeg = camera.jpeg() {
                send(jpeg)
            }
        }
        .preferredColorScheme(.dark)
        .statusBarHidden()
    }

    // MARK: - Layout

    private var topBar: some View {
        HStack {
            Button { dismiss() } label: {
                Image(systemName: "xmark").font(.system(size: 17, weight: .semibold))
                    .frame(width: 44, height: 44).background(Circle().fill(Color.black.opacity(0.5)))
            }
            .accessibilityLabel("Close Hardware Mode")
            Spacer()
            phasePill
            Spacer()
            if !hw.cards.isEmpty {
                Button {
                    hw.cards = []
                } label: {
                    Image(systemName: "xmark.rectangle").font(.system(size: 17, weight: .semibold))
                        .frame(width: 44, height: 44).background(Circle().fill(Color.black.opacity(0.5)))
                }
                .accessibilityLabel("Clear cards")
            }
            Button {
                torch.toggle()
                camera.setTorch(torch)
            } label: {
                Image(systemName: torch ? "flashlight.on.fill" : "flashlight.off.fill")
                    .font(.system(size: 17, weight: .semibold))
                    .frame(width: 44, height: 44).background(Circle().fill(Color.black.opacity(0.5)))
            }
            .accessibilityLabel(torch ? "Light off" : "Light on")
        }
        .foregroundColor(.white)
    }

    /// One status at a time (HWPhase priority); offline doubles as the retry button.
    private var phase: HWPhase {
        HWPhase.current(cameraAuthorized: camera.authorized, connected: hw.connected,
                        researching: hw.researching, thinking: hw.thinking,
                        speaking: voice.speaking, listening: voice.listening,
                        connecting: hw.status == "Connecting…")
    }

    @ViewBuilder
    private var phasePill: some View {
        if phase == .offline {
            Button { reconnect() } label: {
                pill(phase.label, color: .orange).frame(minHeight: 44)
            }
            .accessibilityLabel("Reconnect")
        } else {
            pill(phase.label, color: Self.phaseColor(phase))
        }
    }

    private static func phaseColor(_ phase: HWPhase) -> Color {
        switch phase {
        case .noCamera: return .red
        case .connecting: return .gray
        case .offline: return .orange
        case .researching: return .blue
        case .thinking: return .yellow
        case .speaking, .listening: return .green
        }
    }

    /// Only the current instruction; before the first one, what the owner should say.
    private var instructionCard: some View {
        Text(instructionText)
            .font(.system(size: 18, weight: .semibold))
            .fixedSize(horizontal: false, vertical: true)
            .foregroundColor(.white)
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 14).fill(Color.black.opacity(0.6)))
            .accessibilityElement(children: .combine)
    }

    private var instructionText: String {
        if !hw.say.isEmpty { return hw.say }
        return task.isEmpty ? "Listening. Tell me what we're fixing." : task
    }

    private var bottomBar: some View {
        VStack(spacing: 8) {
            if !voice.heard.isEmpty {
                Text(voice.heard).font(.system(size: 15)).foregroundColor(.white.opacity(0.85))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(spacing: 8) {
                Image(systemName: voice.listening ? "waveform" : "mic.slash")
                    .foregroundColor(voice.listening ? .green : .gray)
                    .frame(width: 32)
                    .accessibilityLabel(voice.listening ? "Listening" : "Not listening")
                if showKeyboard {  // hidden fallback for when speaking is not an option
                    TextField("Or type: done / where is it? / it won't go in", text: $typed)
                        .focused($typing)
                        .submitLabel(.send)
                        .onSubmit { handle(typed); typed = "" }
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.12)))
                }
                Button("Done") { handle("done") }
                    .font(.system(size: 15, weight: .bold))
                    .padding(.horizontal, 14).frame(minHeight: 44)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Color.yellow)).foregroundColor(.black)
                Button {
                    showKeyboard.toggle()
                    typing = showKeyboard
                } label: {
                    Image(systemName: "keyboard").font(.system(size: 17, weight: .semibold))
                        .frame(width: 44, height: 44).background(Circle().fill(Color.black.opacity(0.5)))
                }
                .accessibilityLabel("Type instead")
            }
            .foregroundColor(.white)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16).fill(Color.black.opacity(0.55)))
    }

    private func pill(_ text: String, color: Color) -> some View {
        Text(text).font(.system(size: 13, weight: .semibold)).foregroundColor(.white)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(Capsule().fill(color.opacity(0.75)))
    }

    // MARK: - Behaviour

    private func setUp() {
        let tracker = HWTracker(queue: camera.queue)
        tracker.onUpdate = { rect in
            tracked = rect
            lost = rect == nil
        }
        camera.onPixelBuffer = { @Sendable buffer in tracker.process(buffer) }  // camera queue, not main
        self.tracker = tracker
        camera.start()
        if !initialTask.isEmpty { task = initialTask }
        hw.onSay = { voice.speak($0) }
        // Instant acks play at once; HWVoice queues, never drops, the line that follows.
        hw.onAck = { pcm, rate, text in voice.playPCM(pcm, rate: rate, text: text) }
        hw.onAudio = { pcm, rate, text in voice.playPCM(pcm, rate: rate, text: text) }
        hw.onShape = { shape in
            tracked = nil
            lost = false
            if let shape {
                // Start tracking on the frame the brain looked at (scaled to the camera's size),
                // so the seconds the answer took don't leave the shape on the wrong spot.
                let seen = shape.frameId.flatMap { sentFrames[$0] }.flatMap { CIImage(data: $0) }
                    .map { img in img.transformed(by: CGAffineTransform(scaleX: camera.frameSize.width / img.extent.width,
                                                                         y: camera.frameSize.height / img.extent.height)) }
                tracker.seed(shape.rect, on: seen)
            } else {
                tracker.clear()
            }
        }
        hw.onReady = {
            // A preloaded task starts at once; voice-first sends nothing (the words are coming).
            if let first = HWSession.firstAsk(initialTask: task) {
                sendFreshFrame()
                hw.ask(first)
            }
        }
        hw.onSession = { title in
            sessionPill = title
            let id = UUID()
            sessionPillId = id
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) {  // a hint, not a permanent banner
                if sessionPillId == id { sessionPill = nil }
            }
        }
        voice.onUtterance = { handle($0) }
        guard hw.connect(task: task, sessionId: sessionId) else { return }  // the phase pill says why
        voice.start()  // listening starts with the camera: the first words ARE the task
    }

    private func tearDown() {
        wasConnected = false  // our own disconnect is not a drop
        if hw.connected { hw.sendDiag(voice.diagnostics()) }
        hw.end()  // the gateway posts the report back to the session itself
        // Break the closure -> view -> object cycles so the camera, audio engine and socket are freed.
        hw.onSay = nil
        hw.onAck = nil
        hw.onAudio = nil
        hw.onShape = nil
        hw.onReady = nil
        hw.onSession = nil
        voice.onUtterance = nil
        camera.onPixelBuffer = nil
        tracker?.onUpdate = nil
        hw.disconnect()
        voice.stop()
        camera.setTorch(false)
        camera.stop()
        tracker?.clear()
    }

    private func reconnect() {
        wasConnected = false  // our own reconnect is not a drop; ready sets it again
        _ = hw.connect(task: task, sessionId: sessionId)
    }

    private func send(_ jpeg: Data) {
        let id = hw.sendFrame(jpeg)
        sentFrames[id] = jpeg
        sentFrames = sentFrames.filter { $0.key > id - 4 }
    }

    private func sendFreshFrame() {
        if let jpeg = camera.jpeg() { send(jpeg) }
    }

    private func handle(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        switch HWLocalCommand(t) {
        case .repeatLast?: voice.repeatLine(hw.say)
        case .stop?: voice.stopSpeaking()
        case .torchOn?: torch = true; camera.setTorch(true)
        case .torchOff?: torch = false; camera.setTorch(false)
        case nil:
            lastHeard = t
            voice.stopSpeaking()
            sendFreshFrame()
            hw.ask(t)
        }
    }

    /// Tracking lost the part: ask the brain where it is now, at most every 6 s. The answer
    /// moves the shape; nothing is spoken unless the part is out of view.
    private func relocateIfLost() {
        guard lost, let shape = hw.shape, !hw.thinking, !voice.speaking, voice.heard.isEmpty,
              Date().timeIntervalSince(lastLocate) > 6 else { return }
        lastLocate = Date()
        sendFreshFrame()
        hw.locate(shape.label.isEmpty ? "the part you pointed at" : shape.label)
    }

    /// Watch mode: the picture changed and has been still for 2 s -> ask, at
    /// most every 15 s, whether something is wrong or the instruction is visibly done.
    /// The gateway only lets it move on when the brain marks the work as seen
    /// done in this frame; the brain stays silent ("say": "") when all is fine.
    private func watchIfSettled() {
        guard let changed = lastChange, Date().timeIntervalSince(changed) > 2,
              Date().timeIntervalSince(lastWatch) > 15,
              !hw.say.isEmpty, !hw.thinking, !voice.speaking, voice.heard.isEmpty else { return }
        lastChange = nil
        lastWatch = Date()
        hw.ask("[watch] The picture just changed and is now still. If the image clearly SHOWS the current instruction finished, set \"done_seen\": true and \"next_ready\": true and give the next one. If I did something wrong, warn me. Otherwise reply with \"say\": \"\" and \"shape\": null.", quiet: true)
    }

    /// Image-normalized rect -> view points, matching the preview's aspect-fill crop.
    private func viewRect(_ r: CGRect, in view: CGSize) -> CGRect {
        let img = camera.frameSize
        guard img.width > 0, img.height > 0 else { return .zero }
        let scale = max(view.width / img.width, view.height / img.height)
        let w = img.width * scale, h = img.height * scale
        let ox = (view.width - w) / 2, oy = (view.height - h) / 2
        return CGRect(x: ox + r.minX * w, y: oy + r.minY * h, width: r.width * w, height: r.height * h)
    }
}

/// The AR layer: ONE outline shape in yellow (dark under-stroke), plus a short
/// label tag above the part. Rect -> rounded rect, circle -> ellipse,
/// triangle -> outline triangle.
struct HWShapeLayer: View {
    let shape: HWShape
    let rect: CGRect

    var body: some View {
        ZStack {
            Canvas { ctx, _ in
                let outline = Self.outline(kind: shape.kind, in: rect.insetBy(dx: -4, dy: -4))
                ctx.stroke(outline, with: .color(.black.opacity(0.5)), lineWidth: 6)
                ctx.stroke(outline, with: .color(.yellow), lineWidth: 3)
            }
            if !shape.label.isEmpty {
                tag.position(x: rect.midX, y: max(rect.minY - 26, 30))
            }
        }
    }

    private var tag: some View {
        Text(shape.label)
            .font(.system(size: 15, weight: .bold))
            .foregroundColor(.black)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(Capsule().fill(Color.yellow))
            .fixedSize()
    }

    /// Each outline goes AROUND the part's box, so it never cuts across the part itself.
    static func outline(kind: String, in rect: CGRect) -> Path {
        switch kind {
        case "circle":  // the ellipse through the box's corners (sqrt 2 larger than the box)
            let k = (2.0.squareRoot() - 1) / 2
            return Path(ellipseIn: rect.insetBy(dx: -rect.width * k, dy: -rect.height * k))
        case "triangle":  // apex one box-height above; the box's top corners touch the sides
            var p = Path()
            p.move(to: CGPoint(x: rect.midX, y: rect.minY - rect.height))
            p.addLine(to: CGPoint(x: rect.midX + rect.width, y: rect.maxY))
            p.addLine(to: CGPoint(x: rect.midX - rect.width, y: rect.maxY))
            p.closeSubpath()
            return p
        default: return Path(roundedRect: rect, cornerRadius: 6)  // "rect"
        }
    }
}
