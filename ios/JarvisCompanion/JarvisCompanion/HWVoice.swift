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
    private var bargedIn = false

    func start() {
        SFSpeechRecognizer.requestAuthorization { @Sendable status in
            Task { @MainActor in
                guard status == .authorized else {
                    self.error = "Speech recognition is off: allow it in Settings"
                    return
                }
                self.startEngine()
            }
        }
    }

    func stop() {
        silenceTimer?.invalidate()
        recognition?.cancel()
        recognition = nil
        request = nil
        synth.stopSpeaking(at: .immediate)
        player.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        listening = false
        speaking = false
        KeepAlive.endListening()  // back to the app's normal playback session
    }

    func speak(_ text: String) {
        stopSpeaking()
        bargedIn = false
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05
        speaking = true
        synth.write(utterance) { @Sendable [weak self] buffer in
            guard let pcm = buffer as? AVAudioPCMBuffer, pcm.frameLength > 0 else { return }
            Task { @MainActor in self?.schedule(pcm) }
        }
    }

    func stopSpeaking() {
        synth.stopSpeaking(at: .immediate)
        player.stop()
        pendingBuffers = 0
        speaking = false
        if engine.isRunning { player.play() }
    }

    private func schedule(_ pcm: AVAudioPCMBuffer) {
        guard engine.isRunning, !bargedIn, let converted = convert(pcm) else { return }
        pendingBuffers += 1
        player.scheduleBuffer(converted) { @Sendable [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.pendingBuffers = max(0, self.pendingBuffers - 1)
                if self.pendingBuffers == 0 { self.speaking = false }
            }
        }
        if !player.isPlaying { player.play() }
    }

    private func convert(_ pcm: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
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
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetooth])
            try session.setActive(true)
            let input = engine.inputNode
            try input.setVoiceProcessingEnabled(true)  // echo cancel: must be set before the engine starts
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: playFormat)
            let tap = self.tap  // the tap runs on the audio thread: no main-actor state in it
            input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { @Sendable buffer, _ in
                tap.append(buffer)
            }
            engine.prepare()
            try engine.start()
            player.play()
            listening = true
            beginRecognition()
        } catch {
            self.error = "Audio failed: \(error.localizedDescription)"
        }
    }

    private func beginRecognition() {
        recognition?.cancel()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if recognizer?.supportsOnDeviceRecognition == true { request.requiresOnDeviceRecognition = true }
        self.request = request
        heard = ""
        recognition = recognizer?.recognitionTask(with: request) { @Sendable [weak self] result, error in
            Task { @MainActor in
                guard let self, self.request === request else { return }
                if let result {
                    self.partial(result.bestTranscription.formattedString)
                }
                if error != nil || result?.isFinal == true {
                    // Recognition tasks end on their own (about a minute); keep listening.
                    if self.listening { self.beginRecognition() }
                }
            }
        }
    }

    private func partial(_ text: String) {
        guard !text.isEmpty else { return }
        heard = text
        // Barge-in: real words while Jarvis talks cut it off.
        if speaking, text.split(separator: " ").count >= 2 || text.count >= 5 {
            bargedIn = true
            stopSpeaking()
        }
        silenceTimer?.invalidate()
        silenceTimer = Timer.scheduledTimer(withTimeInterval: 0.9, repeats: false) { [weak self] _ in
            Task { @MainActor in self?.finishUtterance() }
        }
    }

    private func finishUtterance() {
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        heard = ""
        request?.endAudio()
        beginRecognition()
        if !text.isEmpty { onUtterance?(text) }
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
        case "repeat", "say again", "say that again", "again", "what": self = .repeatLast
        case "stop", "quiet", "shut up", "pause": self = .stop
        case "light", "light on", "torch", "torch on", "flashlight on": self = .torchOn
        case "light off", "torch off", "flashlight off": self = .torchOff
        default: return nil
        }
    }
}
