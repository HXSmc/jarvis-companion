import AVFoundation
import Foundation
import Speech
import os

/// Always-on mic tap with a 2 s ring buffer; one wake-word turn at a time.
/// macOS 26 SpeechTranscriber when available, SFSpeechRecognizer otherwise.
@MainActor
final class SpeechInput {
    /// Mic level 0...1 for the orb, throttled to 20 Hz, delivered on main.
    var onLevel: ((Double) -> Void)? {
        didSet { tap.setLevelHandler(onLevel) }
    }

    private let engine = AVAudioEngine()
    private let tap = MicTap()
    private var isRunning = false
    private var currentTurn: Turn?
    private var turnTask: Task<Void, Never>?
    private let log = Logger(subsystem: "JarvisOverlay", category: "SpeechInput")

    init() {}

    // MARK: mic

    /// Starts the always-on input tap and ring buffer. Throws when there is
    /// no usable input device (or mic permission was never granted).
    func start() throws {
        guard !isRunning else { return }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw NSError(domain: "JarvisOverlay.SpeechInput", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "No audio input available"])
        }
        tap.install(on: input, format: format)
        engine.prepare()
        try engine.start()
        isRunning = true
    }

    // MARK: one turn of speech

    /// Replays the ring buffer (so the first words are not lost), then live
    /// audio. Ends 700 ms after the last partial-text change; 5 s with no
    /// speech, an empty final, or 30 s hard cap -> onNothing.
    func listenAfterWake(
        onPartial: @escaping (String) -> Void,
        onFinal: @escaping (_ text: String, _ sttFinalMs: Double, _ endOfSpeech: Date) -> Void,
        onNothing: @escaping () -> Void
    ) {
        guard isRunning else { onNothing(); return }
        cancel()
        let turn = Turn()
        turn.onPartial = onPartial
        turn.onFinal = onFinal
        turn.onNothing = onNothing
        currentTurn = turn
        let partial: @Sendable (String) -> Void = { [weak self] text in
            Task { @MainActor in self?.handlePartial(text, turn: turn) }
        }
        let finalize: @Sendable (String) -> Void = { [weak self] text in
            Task { @MainActor in self?.completeTurn(turn, rawText: text) }
        }
        let nothing: @Sendable () -> Void = { [weak self] in
            Task { @MainActor in self?.completeTurn(turn, nothing: true) }
        }
        let format = engine.inputNode.outputFormat(forBus: 0)
        turnTask = Task { @MainActor [weak self] in
            guard let self else { return }
            #if compiler(>=6.2)
            if #available(macOS 26, *) {
                if await self.runAnalyzer(turn: turn, format: format,
                                          partial: partial, finalize: finalize, nothing: nothing)
                { return }
            }
            #endif
            self.runLegacy(turn: turn, format: format,
                           partial: partial, finalize: finalize, nothing: nothing)
        }
    }

    /// Stops the current recognition turn; the mic tap keeps running.
    func cancel() {
        guard let turn = currentTurn else { return }
        turn.cancelled = true
        currentTurn = nil
        turnTask?.cancel()
        turnTask = nil
        tap.setSink(nil)
        let cancel = turn.cancelHandler
        turn.cancelHandler = nil
        cancel?()
    }

    // MARK: turn state (main actor only)

    private final class Turn: @unchecked Sendable {
        let startedAt = Date()
        var hasSpeech = false
        var lastText = ""
        var endOfSpeech: Date?
        var finished = false
        var cancelled = false
        var cancelHandler: (() -> Void)?
        var onPartial: ((String) -> Void)?
        var onFinal: ((_ text: String, _ sttFinalMs: Double, _ endOfSpeech: Date) -> Void)?
        var onNothing: (() -> Void)?
    }

    private func handlePartial(_ raw: String, turn: Turn) {
        guard !turn.finished, !turn.cancelled else { return }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != turn.lastText else { return }
        turn.lastText = text
        turn.hasSpeech = true
        turn.endOfSpeech = Date()
        turn.onPartial?(text)
    }

    /// First caller wins; a queued callback after cancel stays silent.
    private func completeTurn(_ turn: Turn, nothing: Bool = false, rawText: String? = nil) {
        guard !turn.finished, !turn.cancelled else { return }
        turn.finished = true
        if currentTurn === turn { currentTurn = nil }
        turnTask = nil
        tap.setSink(nil)
        let cancel = turn.cancelHandler
        turn.cancelHandler = nil
        cancel?()
        guard !nothing, let end = turn.endOfSpeech, let raw = rawText else {
            turn.onNothing?()
            return
        }
        let text = Self.stripWake(raw)
        guard !text.isEmpty else { turn.onNothing?(); return }
        log.debug("final \(text, privacy: .private)")
        turn.onFinal?(text, Date().timeIntervalSince(end) * 1000, end)
    }

    /// Drops everything up to a "jarvis" near the start, and stray punctuation.
    static func stripWake(_ raw: String) -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // The 2 s ring buffer can hold words said before the wake word: drop through it.
        if let range = text.range(of: "jarvis", options: [.caseInsensitive]),
           text.distance(from: text.startIndex, to: range.lowerBound) < 40 {
            text = String(text[range.upperBound...])
        }
        return text
            .trimmingCharacters(in: CharacterSet(charactersIn: " ,.!?;:-"))
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: watchdog

    private enum Verdict { case keepWaiting, endOfSpeech, nothing, timedOut, cancelled }

    /// Polls turn state every 50 ms until an end condition fires.
    private func watch(turn: Turn) async -> Verdict {
        while true {
            if turn.cancelled || turn.finished { return .cancelled }
            let now = Date()
            if now.timeIntervalSince(turn.startedAt) >= 30 { return .timedOut }
            if !turn.hasSpeech {
                if now.timeIntervalSince(turn.startedAt) >= 5 { return .nothing }
            } else if let end = turn.endOfSpeech, now.timeIntervalSince(end) >= 0.7 {
                return .endOfSpeech
            }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    // MARK: macOS 26 SpeechAnalyzer

    #if compiler(>=6.2)
    /// Returns false when the new stack is unusable (assets, formats) so the
    /// caller falls back to SFSpeechRecognizer.
    @available(macOS 26, *)
    private func runAnalyzer(
        turn: Turn, format: AVAudioFormat,
        partial: @escaping @Sendable (String) -> Void,
        finalize: @escaping @Sendable (String) -> Void,
        nothing: @escaping @Sendable () -> Void
    ) async -> Bool {
        let transcriber = SpeechTranscriber(
            locale: Locale(identifier: "en-US"),
            transcriptionOptions: [],
            reportingOptions: [.volatileResults],
            attributeOptions: []
        )
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                try await request.downloadAndInstall()
            }
        } catch {
            return false
        }
        guard let target = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]),
              let converter = AVAudioConverter(from: format, to: target)
        else { return false }
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        do {
            try await analyzer.start(inputSequence: stream)
        } catch {
            continuation.finish()
            return false
        }
        let collected = TranscriptBox()
        let resultsTask = Task {
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters)
                    collected.update(text, isFinal: result.isFinal)
                    partial(collected.text)
                }
            } catch {}
        }
        turn.cancelHandler = {
            continuation.finish()
            resultsTask.cancel()
            Task { await analyzer.cancelAndFinishNow() }
        }
        // shortcut: the converter is used from the audio thread only after the ring
        // snapshot below has been fed, so it stays single-threaded.
        let feed: @Sendable (AVAudioPCMBuffer, Date) -> Void = { buffer, _ in
            if let out = Self.convert(converter: converter, from: buffer, to: target) {
                continuation.yield(AnalyzerInput(buffer: out))
            }
        }
        for chunk in tap.ringSnapshot() { feed(chunk.buffer, chunk.at) }
        tap.setSink(feed)
        let verdict = await watch(turn: turn)
        guard !turn.cancelled, !turn.finished else { return true }
        tap.setSink(nil)
        switch verdict {
        case .nothing:
            completeTurn(turn, nothing: true)
        case .endOfSpeech, .timedOut:
            continuation.finish()
            do {
                try await analyzer.finalizeAndFinishThroughEndOfInput()
            } catch {}
            await resultsTask.value
            completeTurn(turn, rawText: collected.text)
        case .cancelled, .keepWaiting:
            break
        }
        return true
    }
    #endif

    // MARK: SFSpeechRecognizer fallback

    private func runLegacy(
        turn: Turn, format: AVAudioFormat,
        partial: @escaping @Sendable (String) -> Void,
        finalize: @escaping @Sendable (String) -> Void,
        nothing: @escaping @Sendable () -> Void
    ) {
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.isAvailable
        else { completeTurn(turn, nothing: true); return }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        let task = recognizer.recognitionTask(with: request) { result, error in
            if let result {
                let text = result.bestTranscription.formattedString
                partial(text)
                if result.isFinal { finalize(text) }
            } else if let error {
                nothing()
            }
        }
        turn.cancelHandler = { task.cancel() }
        for chunk in tap.ringSnapshot() {
            request.appendAudioPCMBuffer(chunk.buffer)
        }
        tap.setSink { [weak request] buffer, _ in
            request?.appendAudioPCMBuffer(buffer)
        }
        Task { @MainActor [weak self] in
            guard let self else { return }
            let verdict = await self.watch(turn: turn)
            guard !turn.cancelled, !turn.finished else { return }
            self.tap.setSink(nil)
            switch verdict {
            case .nothing: self.completeTurn(turn, nothing: true)
            case .endOfSpeech, .timedOut: request.endAudio()
            case .cancelled, .keepWaiting: break
            }
        }
    }

    // MARK: audio helpers

    nonisolated private static func convert(
        converter: AVAudioConverter, from: AVAudioPCMBuffer, to format: AVAudioFormat
    ) -> AVAudioPCMBuffer? {
        let ratio = format.sampleRate / from.format.sampleRate
        let capacity = AVAudioFrameCount((Double(from.frameLength) * ratio).rounded(.up)) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var consumed = false
        let inputBlock: AVAudioConverterInputBlock = { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return from
        }
        var error: NSError?
        let result = converter.convert(to: out, error: &error, withInputFrom: inputBlock)
        guard result != .error, out.frameLength > 0 else { return nil }
        return out
    }
}

// MARK: - transcript accumulator

/// SpeechTranscriber reports finalized segments plus a changing volatile tail.
private final class TranscriptBox: @unchecked Sendable {
    private let lock = NSLock()
    private var finalized = ""
    private var volatile = ""

    func update(_ text: String, isFinal: Bool) {
        lock.lock(); defer { lock.unlock() }
        if isFinal {
            finalized += text
            volatile = ""
        } else {
            volatile = text
        }
    }

    var text: String {
        lock.lock(); defer { lock.unlock() }
        return (finalized + volatile).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - mic tap

/// Tap buffers are only valid inside the callback, so everything is copied.
/// State is lock-guarded: the tap callback runs on the audio thread while
/// sink/level handler swaps and ring snapshots happen on main.
private final class MicTap: @unchecked Sendable {
    private let lock = NSLock()
    private var ring: [(buffer: AVAudioPCMBuffer, at: Date)] = []
    private var sink: (@Sendable (AVAudioPCMBuffer, Date) -> Void)?
    private var levelHandler: ((Double) -> Void)?
    private var lastLevelAt = Date.distantPast

    func install(on node: AVAudioInputNode, format: AVAudioFormat) {
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            let copy = Self.copy(buffer)
            let now = Date()
            var sink: (@Sendable (AVAudioPCMBuffer, Date) -> Void)?
            var handler: ((Double) -> Void)?
            var level: Double?
            lock.lock()
            ring.append((copy, now))
            ring.removeAll { now.timeIntervalSince($0.at) > 2 }
            sink = self.sink
            if now.timeIntervalSince(lastLevelAt) >= 0.05 {
                lastLevelAt = now
                level = Self.rms(copy)
                handler = levelHandler
            }
            lock.unlock()
            if let level, let handler {
                DispatchQueue.main.async { handler(level) }
            }
            sink?(copy, now)
        }
    }

    func setSink(_ newSink: (@Sendable (AVAudioPCMBuffer, Date) -> Void)?) {
        lock.lock()
        sink = newSink
        lock.unlock()
    }

    func setLevelHandler(_ newHandler: ((Double) -> Void)?) {
        lock.lock()
        levelHandler = newHandler
        lock.unlock()
    }

    func ringSnapshot() -> [(buffer: AVAudioPCMBuffer, at: Date)] {
        lock.lock()
        defer { lock.unlock() }
        return ring
    }

    private static func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer {
        guard let out = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength)
        else { return buffer }
        out.frameLength = buffer.frameLength
        let channels = Int(buffer.format.channelCount)
        let floatBytes = Int(buffer.frameLength) * MemoryLayout<Float>.size
        if let src = buffer.floatChannelData, let dst = out.floatChannelData {
            for channel in 0..<channels { memcpy(dst[channel], src[channel], floatBytes) }
        } else if let src = buffer.int16ChannelData, let dst = out.int16ChannelData {
            let int16Bytes = Int(buffer.frameLength) * MemoryLayout<Int16>.size
            for channel in 0..<channels { memcpy(dst[channel], src[channel], int16Bytes) }
        }
        return out
    }

    private static func rms(_ buffer: AVAudioPCMBuffer) -> Double {
        let frames = Int(buffer.frameLength)
        guard frames > 0, let channels = buffer.floatChannelData else { return 0 }
        var sum: Float = 0
        var count = 0
        for channel in 0..<Int(buffer.format.channelCount) {
            let data = channels[channel]
            for frame in 0..<frames {
                let sample = data[frame]
                sum += sample * sample
            }
            count += frames
        }
        guard count > 0 else { return 0 }
        return Double(min(1, sqrt(sum / Float(count))))
    }
}
