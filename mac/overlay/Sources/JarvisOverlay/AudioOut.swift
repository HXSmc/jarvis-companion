import AVFoundation

/// Plays service PCM clips in order and local fallback speech; reports levels/drain.
@MainActor
final class AudioOut {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var synthesizer: AVSpeechSynthesizer?
    private let synthWatcher = SynthWatcher()
    /// Rate of the current player->mixer connection; reconnect when a clip's rate differs.
    private var connectedRate: Int?
    private var pendingBuffers = 0
    private var synthSpeaking = false
    private var tapInstalled = false

    /// 0...1 output RMS while playing (drives the orb).
    var onLevel: ((Double) -> Void)?
    /// First audible output of a turn (PCM or local speech) — latency log hook.
    var onFirstAudio: (() -> Void)?
    /// All scheduled audio finished playing.
    var onDrain: (() -> Void)?

    /// Not player.isPlaying: that stays true after the last buffer played, until stop().
    var isBusy: Bool { pendingBuffers > 0 || synthSpeaking }

    init() {
        engine.attach(player)
        synthWatcher.onFinish = { [weak self] in
            Task { @MainActor in self?.synthFinished() }
        }
    }

    /// 16-bit mono little-endian PCM -> float buffer at `rate`, played in schedule order.
    func enqueue(pcm: Data, rate: Int) {
        let frames = pcm.count / 2
        guard frames > 0 else { return }

        guard let format = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                         sampleRate: Double(rate),
                                         channels: 1,
                                         interleaved: false),
              let buffer = AVAudioPCMBuffer(pcmFormat: format,
                                            frameCapacity: AVAudioFrameCount(frames)),
              let channel = buffer.floatChannelData?[0]
        else { return }

        buffer.frameLength = AVAudioFrameCount(frames)
        pcm.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            let src = raw.bindMemory(to: Int16.self)
            for i in 0..<frames {
                channel[i] = Float(Int16(littleEndian: src[i])) / 32768.0
            }
        }

        if connectedRate != rate {
            if connectedRate != nil { engine.disconnectNodeOutput(player) }
            engine.connect(player, to: engine.mainMixerNode, format: format)
            connectedRate = rate
        }
        if !engine.isRunning {
            do { try engine.start() } catch { return }
        }

        pendingBuffers += 1
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.pendingBuffers = max(self.pendingBuffers - 1, 0)
                if self.pendingBuffers == 0 { self.onDrain?() }
            }
        }
        if !player.isPlaying { player.play() }
        installTapOnce()
        onFirstAudio?()
    }

    /// Local fallback (no service voice, service down, permission prompt).
    func speakLocally(_ text: String) {
        stopPlayer()
        // A new line replaces the one still speaking instead of overlapping it.
        synthesizer?.delegate = nil
        synthesizer?.stopSpeaking(at: .immediate)
        synthSpeaking = false
        let synthesizer = AVSpeechSynthesizer()
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-GB")
            ?? AVSpeechSynthesisVoice(language: "en-US")
        synthesizer.delegate = synthWatcher
        synthesizer.speak(utterance)
        self.synthesizer = synthesizer
        synthSpeaking = true
        onFirstAudio?()
    }

    /// Barge-in on a new wake: silence everything.
    func stop() {
        stopPlayer()
        synthesizer?.stopSpeaking(at: .immediate)
        synthesizer = nil
        synthSpeaking = false
        onLevel?(0)
    }

    // MARK: - Internals

    private func stopPlayer() {
        player.stop()
        pendingBuffers = 0
    }

    private func synthFinished() {
        synthSpeaking = false
        if pendingBuffers == 0 { onDrain?() }
    }

    private func installTapOnce() {
        guard !tapInstalled else { return }
        tapInstalled = true
        engine.mainMixerNode.installTap(onBus: 0, bufferSize: 512, format: nil) { [weak self] buffer, _ in
            guard let channel = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
            let n = Int(buffer.frameLength)
            var sum: Float = 0
            for i in 0..<n {
                let v = channel[i]
                sum += v * v
            }
            let level = Double(min(max((sum / Float(n)).squareRoot() * 3, 0), 1))
            Task { @MainActor in self?.onLevel?(level) }
        }
    }
}

/// AVSpeechSynthesizerDelegate bridge (delegate is not MainActor-annotated).
private final class SynthWatcher: NSObject, AVSpeechSynthesizerDelegate {
    var onFinish: (() -> Void)?

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        onFinish?()
    }
}
