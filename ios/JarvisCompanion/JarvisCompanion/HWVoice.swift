import AVFoundation
import Speech

/// Hands-free voice for Hardware Mode: always listening, talking back, and
/// stopping the moment the user talks over it (barge-in).
///
/// Speech is rendered with `AVSpeechSynthesizer.write` into a player node on
/// the SAME engine as the mic, with voice processing on, so the phone's echo
/// canceller removes Jarvis's own voice from the mic.
@MainActor
final class HWVoice: ObservableObject {
    @Published var listening = false
    @Published var heard = ""
    @Published var speaking = false
    @Published var error: String?

    /// A finished utterance (after a short pause).
    var onUtterance: ((String) -> Void)?

    /// Words the recognizer should favour: short one-word commands get misheard in a noisy
    /// room ("done" -> "John") without them. Found by the synthetic STT tests.
    static let commandHints = ["done", "I'm done", "finished", "next", "next step", "repeat", "say again",
                               "stop", "wait", "light on", "light off", "where is", "SATA", "cable", "port",
                               "خلصت", "تم", "خلاص", "التالي", "عيد", "وقف"]

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let synth = AVSpeechSynthesizer()
    private let playFormat = AVAudioFormat(standardFormatWithSampleRate: 22050, channels: 1)!
    private var converters: [String: AVAudioConverter] = [:]
    private let recognizer = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest? {
        didSet { tap.set(request) }
    }
    private let tap = TapTarget()
    private var recognition: SFSpeechRecognitionTask?
    private var silenceTimer: Timer?
    private var pendingBuffers = 0
    /// Bumped on every stop: late buffers / completions from an older utterance are ignored.
    private var generation = 0
    private var stopped = false
    private var failures = 0
    /// Speech that arrived before the engine ran: spoken as soon as it does.
    private var pendingSpeech: String?
    /// What Jarvis last said, and when it stopped: the mic hearing it back is not the user.
    private var lastSpoken = ""
    private var spokeUntil = Date.distantPast
    private var voiceProcessing = false
    private var buffersThisSpeech = 0
    private var counts: [String: Int] = [:]
    private var events: [String] = []
    private let startedAt = Date()

    func start() {
        stopped = false
        SFSpeechRecognizer.requestAuthorization { @Sendable status in
            Task { @MainActor in
                guard !self.stopped else { return }  // the screen closed while the prompt was up
                guard status == .authorized else {
                    self.fail("Speech recognition is off: allow it in Settings")
                    return
                }
                // Speech permission is not mic permission: without this the mic records silence.
                AVAudioSession.sharedInstance().requestRecordPermission { @Sendable granted in
                    Task { @MainActor in
                        guard !self.stopped else { return }
                        guard granted else {
                            self.fail("Microphone is off: allow it in Settings")
                            return
                        }
                        self.startEngine()
                    }
                }
            }
        }
    }

    /// Voice state for the gateway log, so a silent failure on the phone can be seen from the PC.
    func diagnostics() -> [String: Any] {
        let session = AVAudioSession.sharedInstance()
        let mic: String
        switch session.recordPermission {
        case .granted: mic = "granted"
        case .denied: mic = "denied"
        default: mic = "undetermined"
        }
        return [
            "speechAuth": SFSpeechRecognizer.authorizationStatus().rawValue,
            "mic": mic,
            "engine": engine.isRunning,
            "voiceProcessing": voiceProcessing,
            "listening": listening,
            "speaking": speaking,
            "category": session.category.rawValue,
            "mode": session.mode.rawValue,
            "outputs": session.currentRoute.outputs.map { $0.portType.rawValue },
            "inputs": session.currentRoute.inputs.map { $0.portType.rawValue },
            "outputVolume": session.outputVolume,
            "recognizer": recognizer?.locale.identifier ?? "none",
            "recognizerAvailable": recognizer?.isAvailable ?? false,
            "error": error ?? "",
            "counts": counts,
            "events": events,
        ]
    }

    private func note(_ event: String) {
        events.append(String(format: "%.1f %@", Date().timeIntervalSince(startedAt), String(event.prefix(120))))
        if events.count > 20 { events.removeFirst(events.count - 20) }
    }

    private func count(_ key: String) {
        counts[key, default: 0] += 1
    }

    private func fail(_ message: String) {
        error = message
        note("error: \(message)")
    }

    func stop() {
        stopped = true
        listening = false
        silenceTimer?.invalidate()
        recognition?.cancel()
        recognition = nil
        request = nil
        generation += 1
        synth.stopSpeaking(at: .immediate)
        player.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        speaking = false
        KeepAlive.endListening()  // back to the app's normal playback session
    }

    func speak(_ text: String) {
        count("speak")
        guard engine.isRunning else {
            pendingSpeech = text
            note("speak queued: engine not running")
            return
        }
        stopSpeaking()
        lastSpoken = text
        spokeUntil = Date().addingTimeInterval(30)  // until the last buffer finishes
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05
        let gen = generation
        buffersThisSpeech = 0
        synth.write(utterance) { @Sendable [weak self] buffer in
            guard let pcm = buffer as? AVAudioPCMBuffer, pcm.frameLength > 0 else { return }
            Task { @MainActor in self?.schedule(pcm, generation: gen) }
        }
        // write() can hand back no audio at all; then speak through the system voice instead
        // (loses echo cancel for this line, but the user hears it).
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            Task { @MainActor in self?.speakDirectlyIfSilent(text, generation: gen) }
        }
    }

    private func speakDirectlyIfSilent(_ text: String, generation gen: Int) {
        guard gen == generation, buffersThisSpeech == 0, !stopped else { return }
        count("speakFallback")
        note("write() gave no audio: speaking directly")
        synth.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05
        synth.speak(utterance)
        spokeUntil = Date().addingTimeInterval(Double(text.count) / 12 + 1.5)  // ~12 chars/s, for the echo filter
    }

    func stopSpeaking() {
        generation += 1
        synth.stopSpeaking(at: .immediate)
        player.stop()
        pendingBuffers = 0
        if speaking { spokeUntil = Date().addingTimeInterval(1.5) }
        speaking = false
        if engine.isRunning { player.play() }
    }

    private func schedule(_ pcm: AVAudioPCMBuffer, generation gen: Int) {
        guard gen == generation, engine.isRunning, let converted = convert(pcm) else { return }
        pendingBuffers += 1
        buffersThisSpeech += 1
        count("buffers")
        speaking = true
        player.scheduleBuffer(converted) { @Sendable [weak self] in
            Task { @MainActor in
                guard let self, gen == self.generation else { return }
                self.pendingBuffers = max(0, self.pendingBuffers - 1)
                if self.pendingBuffers == 0 {
                    self.speaking = false
                    self.spokeUntil = Date().addingTimeInterval(1.5)  // room echo / late recognition
                }
            }
        }
        if !player.isPlaying { player.play() }
    }

    /// To the player's format (22.05 kHz mono float). Internal for the audio tests.
    func convert(_ pcm: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        if pcm.format == playFormat { return pcm }
        let key = pcm.format.description
        let converter = converters[key] ?? AVAudioConverter(from: pcm.format, to: playFormat)
        guard let converter else { return nil }
        converters[key] = converter
        let capacity = AVAudioFrameCount(Double(pcm.frameLength) * playFormat.sampleRate / pcm.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: capacity) else { return nil }
        var fed = false
        var err: NSError?
        _ = converter.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return pcm
        }
        return err == nil ? out : nil
    }

    private func startEngine() {
        do {
            try runEngine(voiceProcessing: true)
        } catch {
            // Echo cancel is not available on every route/device: hearing and speaking matter more.
            note("voice processing failed: \(error.localizedDescription)")
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
            try? engine.inputNode.setVoiceProcessingEnabled(false)
            do {
                try runEngine(voiceProcessing: false)
            } catch {
                fail("Audio failed: \(error.localizedDescription)")
                return
            }
        }
        listening = true
        note("engine on, voiceProcessing=\(voiceProcessing), out=\(AVAudioSession.sharedInstance().currentRoute.outputs.map { $0.portType.rawValue })")
        beginRecognition()
        if let text = pendingSpeech {
            pendingSpeech = nil
            speak(text)
        }
    }

    private func runEngine(voiceProcessing on: Bool) throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: on ? .voiceChat : .default,
                                options: [.defaultToSpeaker, .allowBluetooth])
        try session.setActive(true)
        let input = engine.inputNode
        if on { try input.setVoiceProcessingEnabled(true) }  // echo cancel: must be set before the engine starts
        voiceProcessing = on
        if player.engine == nil { engine.attach(player) }
        engine.connect(player, to: engine.mainMixerNode, format: playFormat)
        let tap = self.tap  // the tap runs on the audio thread: no main-actor state in it
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { @Sendable buffer, _ in
            tap.append(buffer)
        }
        engine.prepare()
        try engine.start()
        player.play()
    }

    private func beginRecognition() {
        guard listening else { return }
        recognition?.cancel()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true  // server or on-device, the same as the Home mic, which works
        request.contextualStrings = Self.commandHints
        self.request = request
        heard = ""
        let current = ObjectIdentifier(request)  // compare ids: the request itself isn't Sendable
        recognition = recognizer?.recognitionTask(with: request) { @Sendable [weak self] result, error in
            Task { @MainActor in
                guard let self, self.request.map(ObjectIdentifier.init) == current else { return }
                if let result {
                    self.partial(result.bestTranscription.formattedString)
                }
                guard error != nil || result?.isFinal == true else { return }
                if !self.heard.isEmpty {
                    self.finishUtterance()  // don't lose words when a task ends on its own
                } else if let error {
                    self.note("recognition error: \(error.localizedDescription)")
                    self.retryAfterFailure()
                } else {
                    self.beginRecognition()  // tasks end on their own (about a minute)
                }
            }
        }
    }

    /// Back off when recognition keeps failing (Siri off, no speech assets) instead of spinning.
    private func retryAfterFailure() {
        failures += 1
        guard failures <= 5 else {
            fail("Speech recognition keeps failing. Type instead, or check Siri & Dictation.")
            return
        }
        let delay = min(0.5 * pow(2, Double(failures)), 10)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            Task { @MainActor in self?.beginRecognition() }
        }
    }

    private func partial(_ text: String) {
        guard !text.isEmpty else { return }
        failures = 0
        count("partials")
        heard = text
        // Barge-in: real words while Jarvis talks cut it off, but not Jarvis's own voice coming back.
        if speaking, text.split(separator: " ").count >= 2 || text.count >= 5, !isEcho(text) {
            count("bargeIn")
            note("barge-in: \(text)")
            stopSpeaking()
        }
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: 0.9, repeats: false) { [weak self] _ in
            Task { @MainActor in self?.finishUtterance() }
        }
    }

    private func finishUtterance() {
        silenceTimer?.invalidate()
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        heard = ""
        request?.endAudio()
        beginRecognition()
        guard !text.isEmpty else { return }
        if isEcho(text) {
            count("echoDropped")
            note("echo dropped: \(text)")
            return
        }
        count("utterances")
        note("heard: \(text)")
        onUtterance?(text)
    }

    /// The mic picked up Jarvis's own speech (no or weak echo cancel): most of the words are
    /// ones Jarvis just said.
    private func isEcho(_ text: String) -> Bool {
        guard speaking || Date() < spokeUntil else { return false }
        return HWVoice.mostlyContained(text, in: lastSpoken)
    }

    nonisolated static func mostlyContained(_ text: String, in spoken: String) -> Bool {
        // The recognizer spells numbers out ("SATA1" -> "sat one", "Step 1 of 2" -> "step one two"):
        // split letters from digits and read digits as words before comparing.
        let names = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
        func words(_ s: String) -> [String] {
            var spaced = ""
            for ch in s.lowercased() {
                if let d = ch.wholeNumberValue, ch.isASCII { spaced += " \(names[d]) " } else { spaced.append(ch) }
            }
            return spaced.components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }
        }
        let heard = words(text)
        guard !heard.isEmpty else { return false }
        let said = Set(words(spoken))
        let hits = heard.filter { said.contains($0) }.count
        return Double(hits) / Double(heard.count) >= 0.7
    }
}

/// The live recognition request, reachable from the audio thread.
private final class TapTarget: @unchecked Sendable {
    private let lock = NSLock()
    private var request: SFSpeechAudioBufferRecognitionRequest?

    func set(_ request: SFSpeechAudioBufferRecognitionRequest?) {
        lock.lock(); self.request = request; lock.unlock()
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock(); let request = self.request; lock.unlock()
        request?.append(buffer)
    }
}

/// Short commands handled on the phone, no AI call.
enum HWLocalCommand {
    case repeatLast, stop, torchOn, torchOff

    init?(_ text: String) {
        let t = text.lowercased().trimmingCharacters(in: .punctuationCharacters.union(.whitespaces))
        switch t {
        case "repeat", "say again", "say that again", "again", "what", "عيد", "عيدها", "مرة ثانية": self = .repeatLast
        case "stop", "quiet", "shut up", "pause", "وقف", "اسكت", "بس": self = .stop
        case "light", "light on", "torch", "torch on", "flashlight on", "نور", "شغل النور", "الكشاف": self = .torchOn
        case "light off", "torch off", "flashlight off", "طفي النور", "طف النور": self = .torchOff
        default: return nil
        }
    }
}
