import AVFoundation
import OverlayCore

/// Plays service PCM clips in order and local fallback speech; reports levels/drain.
@MainActor
final class AudioOut {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var synthesizer: AVSpeechSynthesizer?
    private let synthWatcher = SynthWatcher()
    /// Rate of the current player->mixer connection; reconnect when a clip's rate differs.
    private var connectedRate: Int?
    /// Unplayed buffers right now; barge-in drops them (debug note dropped_sentences).
    private(set) var pendingBuffers = 0
    private var synthSpeaking = false
    private var tapInstalled = false

    /// 0...1 output RMS while playing (drives the orb).
    var onLevel: ((Double) -> Void)?
    /// First audible output of a turn (PCM or local speech) — latency log hook.
    var onFirstAudio: (() -> Void)?
    /// All scheduled audio finished playing.
    var onDrain: (() -> Void)?
    /// When the last drain fired; the follow-up replay window starts here (plus echo tail).
    private(set) var lastDrainAt: Date?
    /// Raw playback RMS per tap buffer with its time (last ~5 s); the barge-in echo gate reads it.
    private let outLevels = OutputLevels()

    var outputLevels: [LevelSample] { outLevels.snapshot() }

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
                if self.pendingBuffers == 0 { self.lastDrainAt = Date(); self.onDrain?() }
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
        if pendingBuffers == 0 { lastDrainAt = Date(); onDrain?() }
    }

    private func installTapOnce() {
        guard !tapInstalled else { return }
        tapInstalled = true
        // The tap runs on the audio thread: it gets the ring directly, not through the actor.
        let levels = outLevels
        engine.mainMixerNode.installTap(onBus: 0, bufferSize: 512, format: nil) { [weak self] buffer, _ in
            guard let channel = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
            let n = Int(buffer.frameLength)
            var sum: Float = 0
            for i in 0..<n {
                let v = channel[i]
                sum += v * v
            }
            let rms = Double((sum / Float(n)).squareRoot())
            levels.append(LevelSample(at: Date(), rms: rms, frames: n))
            let level = Double(min(max(rms * 3, 0), 1))
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

/// Lock-guarded raw output-RMS ring (~5 s): appended on the audio thread, read on the main actor.
private final class OutputLevels: @unchecked Sendable {
    private let lock = NSLock()
    private var samples: [LevelSample] = []

    func append(_ sample: LevelSample) {
        lock.lock()
        samples.append(sample)
        let cutoff = sample.at.addingTimeInterval(-5)
        while let first = samples.first, first.at < cutoff { samples.removeFirst() }
        lock.unlock()
    }

    func snapshot() -> [LevelSample] {
        lock.lock()
        defer { lock.unlock() }
        return samples
    }
}
