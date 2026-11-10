import Foundation
import OverlayCore

@MainActor
final class AppController {
    private let model = OverlayModel()
    private let client = ServiceClient()
    private let audio = AudioOut()
    private let speech = SpeechInput()
    private var overlayPanel: OverlayPanel?
    private var cardPanel: CardPanel?

    // Conversation/turn state (latency log).
    private var convId = UUID().uuidString
    private var lastConvUse = Date.distantPast
    private var turnEndOfSpeech: Date?
    private var turnSttFinalMs: Double = 0
    private var turnConvId = ""
    private var turnFirstAudio = false
    private var turnDone = false

    // Outage state.
    private var serviceVoice: String?
    private var outageAnnounced = false
    private var bannerTask: Task<Void, Never>?
    private var permissionTimer: Task<Void, Never>?

    func start() {
        let overlay = OverlayPanel()
        overlay.host(OverlayRootView(model: model))
        let cards = CardPanel()
        cards.host(model)
        overlayPanel = overlay
        cardPanel = cards

        audio.onLevel = { [weak self] level in self?.model.level = level }
        audio.onFirstAudio = { [weak self] in self?.markFirstAudio() }
        audio.onDrain = { [weak self] in self?.handleAudioDrained() }
        speech.onLevel = { [weak self] level in self?.model.level = level }

        Task { await launch() }
    }

    // MARK: - Launch

    private func launch() async {
        await requestPermissionsIfNeeded()
        do {
            try speech.start()
        } catch {
            model.orb = .error
            let line = "Microphone unavailable. Check System Settings > Privacy & Security > Microphone."
            model.banner = line
            audio.speakLocally(line)
        }
        await runEventsLoop()
    }

    private func requestPermissionsIfNeeded() async {
        let missing = Permissions.missing()
        guard !missing.isEmpty else { return }
        for permission in missing {
            _ = await Permissions.request(permission)
        }
        refreshPermissions()
        guard !model.missingPermissions.isEmpty else { return }

        model.orb = .error
        speakPermissionNeed()
    }

    /// Keeps the permission card in sync; re-checks every 10 s while any are missing.
    private func refreshPermissions() {
        let missing = Permissions.missing()
        if missing != model.missingPermissions { model.missingPermissions = missing }
        if missing.isEmpty {
            permissionTimer?.cancel()
            permissionTimer = nil
        } else if permissionTimer == nil {
            permissionTimer = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 10_000_000_000)
                    guard !Task.isCancelled, let self else { return }
                    self.refreshPermissions()
                }
            }
        }
    }

    private func speakPermissionNeed() {
        let names = model.missingPermissions.map { Permissions.paneName($0) }.joined(separator: " and ")
        audio.speakLocally("I need \(names) permission. I've put the steps on screen.")
    }

    /// Banner that clears itself after `seconds`; the outage banner is sticky, not timed.
    private func showBanner(_ text: String, for seconds: Double) {
        bannerTask?.cancel()
        model.banner = text
        bannerTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.model.banner = nil
        }
    }

    // MARK: - Events loop (wake) and outage handling

    private func runEventsLoop() async {
        while true {
            do {
                let health = try await client.health()
                serviceVoice = health.voice
                endOutage()
                for try await name in client.events() {
                    endOutage()
                    if name == "wake" { await handleWake() }
                }
                enterOutage()  // stream ended early = service gone
            } catch {
                enterOutage()
            }
            try? await Task.sleep(nanoseconds: 5_000_000_000)
        }
    }

    private func enterOutage() {
        bannerTask?.cancel()
        bannerTask = nil
        if !outageAnnounced {
            outageAnnounced = true
            audio.speakLocally("My service is not running.")
        }
        model.orb = .error
        model.banner = "Jarvis service is not running"
    }

    private func endOutage() {
        guard outageAnnounced else { return }
        outageAnnounced = false
        bannerTask?.cancel()
        bannerTask = nil
        model.banner = nil
        model.orb = .idle
    }

    private func handleWake() async {
        refreshPermissions()
        if !model.missingPermissions.isEmpty { speakPermissionNeed() }
        model.orb = .listening
        audio.stop()  // barge-in
        // Screen capture starts at wake; only awaited when an utterance lands.
        let screenTask = Task { await ScreenContext.capture() }
        let listenStart = Date()
        speech.listenAfterWake(
            onPartial: { _ in },
            onFinal: { [weak self] text, sttFinalMs, endOfSpeech in
                Task { @MainActor in
                    await self?.handleUtterance(text: text,
                                          sttFinalMs: sttFinalMs,
                                          audioMs: endOfSpeech.timeIntervalSince(listenStart) * 1000,
                                          endOfSpeech: endOfSpeech,
                                          screenTask: screenTask)
                }
            },
            onNothing: { [weak self] in
                Task { @MainActor in
                    guard let self else { return }
                    self.client.debugNote(step: "no_speech", detail: "speech-to-text heard no words after the wake")
                    // With no mic there is no speech to catch; the permission line covers it.
                    if !self.model.missingPermissions.contains(.microphone) {
                        self.audio.speakLocally("I didn't catch that.")
                    }
                    self.model.orb = .idle
                }
            })
    }

    // MARK: - Utterance turn

    private func handleUtterance(text: String,
                                 sttFinalMs: Double,
                                 audioMs: Double,
                                 endOfSpeech: Date,
                                 screenTask: Task<ScreenPayload?, Never>) async {
        model.orb = .thinking
        let screen = await screenTask.value

        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized == "clear" || normalized == "clear the screen" {
            model.clear()
        }

        let conv = conversationID()
        turnEndOfSpeech = endOfSpeech
        turnSttFinalMs = sttFinalMs
        turnConvId = conv
        turnFirstAudio = false
        turnDone = false

        do {
            for try await event in client.utterance(text: text, convId: conv,
                                                    sttFinalMs: sttFinalMs, audioMs: audioMs,
                                                    screen: screen) {
                handle(event)
            }
        } catch {
            // A 401/500/timeout never surfaces on the events loop; say it out loud.
            client.debugNote(step: "app_error", detail: "utterance request failed: \(error)")
            audio.speakLocally("I couldn't reach my service. Try again.")
            showBanner("Couldn't reach Jarvis service", for: 5)
            model.orb = .error  // until the next wake sets .listening
        }
    }

    private func handle(_ event: OverlayEvent) {
        switch event {
        case .say(let text):
            model.activity()
            if serviceVoice == nil { audio.speakLocally(text) }
        case .audio(_, let rate, let pcm):
            model.activity()
            model.orb = .speaking
            audio.enqueue(pcm: pcm, rate: rate)
        case .shape(let shape, let check):
            model.activity()
            var updated = shape
            if let check,
               let fresh = ScreenContext.recheck(role: check.role, label: check.label ?? "", near: shape.rect) {
                updated.box = [Double(fresh.minX), Double(fresh.minY), Double(fresh.maxX), Double(fresh.maxY)]
            }
            model.add(shape: updated)
        case .card(let card):
            model.activity()
            model.add(card: card)
        case .lines(_, let ranges):
            model.activity()
            highlightLines(ranges)
        case .clear:
            model.clear()
        case .done:
            if audio.isBusy { turnDone = true } else { model.orb = .idle }
        case .unknown:
            break
        }
    }

    private func highlightLines(_ ranges: [ClosedRange<Int>]) {
        Task { [weak self] in
            // 900 ms: let the editor settle before the gutter OCR pass.
            try? await Task.sleep(nanoseconds: 900_000_000)
            guard !Task.isCancelled, let self else { return }
            for band in await ScreenContext.lineBands(ranges: ranges) {
                self.model.add(shape: HUDShape(kind: .rect,
                                               box: [Double(band.minX), Double(band.minY),
                                                     Double(band.maxX), Double(band.maxY)],
                                               label: "changed"))
            }
        }
    }

    // MARK: - Latency log

    /// Same conversation id for 5 minutes, then a fresh UUID.
    private func conversationID() -> String {
        let now = Date()
        if now.timeIntervalSince(lastConvUse) > 300 { convId = UUID().uuidString }
        lastConvUse = now
        return convId
    }

    private func markFirstAudio() {
        guard let endOfSpeech = turnEndOfSpeech, !turnFirstAudio else { return }
        turnFirstAudio = true
        writeLatencyLine(endOfSpeech: endOfSpeech, firstAudio: Date())
    }

    private func writeLatencyLine(endOfSpeech: Date, firstAudio: Date) {
        let iso = ISO8601DateFormatter().string(from: firstAudio)
        let eosMs = Int(firstAudio.timeIntervalSince(endOfSpeech) * 1000)
        let line = "\(iso) eos_to_first_audio_ms=\(eosMs) stt_final_ms=\(Int(turnSttFinalMs)) conv=\(turnConvId)\n"

        let files = FileManager.default
        let dir = files.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/JarvisOverlay")
        try? files.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("latency.log")
        if let handle = FileHandle(forWritingAtPath: url.path) {
            defer { try? handle.close() }
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
        } else {
            try? line.write(to: url, atomically: true, encoding: .utf8)
        }
    }

    private func handleAudioDrained() {
        guard turnDone else { return }
        turnDone = false
        model.orb = .idle
    }
}
