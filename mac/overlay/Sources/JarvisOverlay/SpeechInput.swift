import AVFoundation
import Foundation
import Speech
import os

import OverlayCore

/// Always-on mic tap with a 2 s ring buffer; one wake-word turn at a time.
/// macOS 26 SpeechTranscriber when available, SFSpeechRecognizer otherwise.
@MainActor
final class SpeechInput {
    private let engine = AVAudioEngine()
    private let tap = MicTap()
    private var isRunning = false
    private var currentTurn: Turn?
    private var turnTask: Task<Void, Never>?
    /// How the last finished turn was heard (STT path, counters, what ended it): debug page only.
    private(set) var lastDiagnosis = ""
    private let log = Logger(subsystem: "JarvisOverlay", category: "SpeechInput")
    /// TailGate's ratio; the service sends it on /overlay/health (OVERLAY_TAIL_LEVEL_RATIO).
    var tailRatio = TailGate.defaultRatio
    /// Barge-in with the AEC-backed gate wanted (health barge_aec); the start() probe decides.
    var aecEnabled = false
    /// The voice-processing probe actually stuck; the gate uses the AEC margin only when true.
    private(set) var aecActive = false

    /// What the service's follow-up gate reads with the words: 100 ms mic levels, STT confidence.
    struct Heard {
        var levels: [Double]
        var conf: Double?
    }

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
        // AEC must be enabled before the engine starts; any failure keeps today's plain path.
        if aecEnabled {
            do { try input.setVoiceProcessingEnabled(true); aecActive = true }
            catch { aecActive = false }
        }
        tap.install(on: input, format: format)
        engine.prepare()
        do { try engine.start() } catch { aecActive = false; throw error }
        isRunning = true
    }

    // MARK: one turn of speech

    /// Replays the ring buffer (so the first words are not lost), then live
    /// audio. Ends per TurnEndpointer (0.7 s quiet, 2.7 s when the words trail off);
    /// 5 s with no speech, an empty final, or 30 s hard cap -> onNothing(why), why = a short
    /// diagnosis for the debug page (STT path, buffers fed, results, errors).
    /// followUp: the open-mic window after a reply. Replays only ring audio stamped after
    /// replayFrom (the reply's drain + echo tail); nil replays nothing. 8 s to start speaking.
    /// barge: listening during reply playback (barge-in); runs until confirmed or cancelled.
    func listenAfterWake(
        followUp: Bool = false,
        replayFrom: Date? = nil,
        barge: BargeRun? = nil,
        onPartial: @escaping (String) -> Void,
        onFinal: @escaping (_ text: String, _ sttFinalMs: Double, _ endOfSpeech: Date, _ heard: Heard) -> Void,
        onNothing: @escaping (_ why: String) -> Void
    ) {
        guard isRunning else { onNothing("mic engine not running"); return }
        cancel()
        let turn = Turn()
        turn.followUp = followUp
        turn.replayFrom = replayFrom
        turn.tail = TailGate(ratio: tailRatio)
        if let barge {
            turn.barge = barge
            barge.gateThrough = turn.startedAt
        }
        // Levels cover the audio the recogniser hears: the replayed ring (wake) or post-reply audio.
        turn.levelFrom = followUp ? (replayFrom.map { max($0, turn.startedAt.addingTimeInterval(-2)) } ?? turn.startedAt)
                                  : turn.startedAt.addingTimeInterval(-2)
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
        turn.barge?.end()
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
        /// The 30 s cap and the no-speech wait count from here: the start, or a barge-in's
        /// confirmation (a barge run can be 30 s into a reply when the owner speaks).
        var capFrom = Date()
        var hasSpeech = false
        var lastText = ""
        var endOfSpeech: Date?
        var finished = false
        var cancelled = false
        var cancelHandler: (() -> Void)?
        var onPartial: ((String) -> Void)?
        var onFinal: ((_ text: String, _ sttFinalMs: Double, _ endOfSpeech: Date, _ heard: Heard) -> Void)?
        var onNothing: ((String) -> Void)?
        var followUp = false
        var replayFrom: Date?  // follow-up turns replay only ring chunks at/after the reply drain + echo tail
        var barge: BargeRun?  // listening during reply playback; gate + confirm state (barge-in)
        var path = ""  // STT path + why it was taken, for the debug page
        var endNote = ""  // how long the end-of-turn silence was, for the debug page
        let stats = FeedStats()
        var tail = TailGate()
        var levelFrom = Date()
        var fedBlocks = 0  // finished level blocks already given to `tail`
        var keptText: String?  // set when room words after a complete request were cut
        let confidence = ConfidenceBox()

        var diagnosis: String { "\(path)\(endNote); \(stats.summary)" }
    }

    private func handlePartial(_ raw: String, turn: Turn) {
        guard !turn.finished, !turn.cancelled else { return }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != turn.lastText else { return }
        turn.lastText = text
        turn.hasSpeech = true
        turn.endOfSpeech = Date()
        if let barge = turn.barge, !barge.confirmed { checkBarge(barge, turn: turn, partial: text, now: Date()) }
        turn.tail.partial(text, at: Date())
        turn.onPartial?(text)
    }

    /// The barge-in word check on one partial: gate candidate (or one that ended within the
    /// partials' lag) + words the reply does not contain, counted only from the words heard since
    /// the gate last went over the echo. On confirm the turn goes on as an ordinary follow-up.
    private func checkBarge(_ barge: BargeRun, turn: Turn, partial: String, now: Date) {
        feedGate(barge, now: now)  // the gate is current before it is read
        barge.partials += 1
        guard barge.gate.recentlyOver(at: now) else {
            // Echo only so far: these words are the reply's, the owner's can only come after them.
            barge.freshFrom = BargeWords.wordCount(partial)
            return
        }
        let candidate = barge.gate.candidate(at: now)
        if candidate { barge.checked += 1 }
        guard let reason = BargeDecision.decide(gateCandidate: candidate, partial: partial,
                                                reply: barge.reply, wake: false,
                                                enabled: barge.enabled, aec: barge.aec,
                                                freshFrom: barge.freshFrom) else { return }
        barge.confirmed = true
        turn.capFrom = now
        // The tail gate and the sent levels start at the confirmation, not over the reply's echo.
        turn.tail = TailGate(ratio: tailRatio)
        turn.fedBlocks = 0
        turn.levelFrom = Date()
        barge.onConfirmed?(reason)
        barge.end()
    }

    /// First caller wins; a queued callback after cancel stays silent.
    private func completeTurn(_ turn: Turn, nothing: Bool = false, rawText: String? = nil) {
        guard !turn.finished, !turn.cancelled else { return }
        turn.finished = true
        lastDiagnosis = turn.diagnosis
        if currentTurn === turn { currentTurn = nil }
        turnTask = nil
        tap.setSink(nil)
        let cancel = turn.cancelHandler
        turn.cancelHandler = nil
        cancel?()
        // A2: an unconfirmed barge turn never calls onFinal/onNothing (the legacy recogniser's
        // time-cap final holds Jarvis's own echo); it just ends and the reply plays on.
        if let barge = turn.barge, !barge.confirmed { barge.end(); return }
        guard !nothing, let end = turn.endOfSpeech, let raw = turn.keptText ?? rawText else {
            turn.onNothing?(turn.diagnosis)
            return
        }
        let text = Self.stripWake(raw)
        guard !text.isEmpty else { turn.onNothing?("only the wake word was heard; \(turn.diagnosis)"); return }
        log.debug("final \(text, privacy: .private)")
        let levels = LevelBlocks.blocks(tap.levelSamples(), from: turn.levelFrom, to: end.addingTimeInterval(0.3))
        turn.onFinal?(text, Date().timeIntervalSince(end) * 1000, end,
                      Heard(levels: Array(levels.suffix(LevelBlocks.maxSent)), conf: turn.confidence.mean))
    }

    /// Drops everything up to a "jarvis" near the start, and stray punctuation.
    static func stripWake(_ raw: String) -> String { TurnEndpointer.stripWake(raw) }

    // MARK: watchdog

    private enum Verdict { case keepWaiting, endOfSpeech, nothing, timedOut, cancelled }

    /// Polls turn state every 50 ms until an end condition fires.
    private func watch(turn: Turn) async -> Verdict {
        while true {
            if turn.cancelled || turn.finished { return .cancelled }
            // Barge listening runs while the reply plays: silence, the caps and the endpointer
            // never end it — only a confirmed barge-in or a cancel (the reply drain) does.
            if let barge = turn.barge, !barge.confirmed {
                feedGate(barge, now: Date())
                try? await Task.sleep(nanoseconds: 50_000_000)
                continue
            }
            let now = Date()
            if now.timeIntervalSince(turn.capFrom) >= 30 { return .timedOut }
            feedTail(turn, now: now)
            if !turn.hasSpeech {
                if now.timeIntervalSince(turn.capFrom) >= (turn.followUp ? 8 : 5) { return .nothing }
            } else if !turn.followUp, let kept = turn.tail.cut(text: turn.lastText, now: now) {
                // Wake turns only (round-6 item 2): a follow-up may answer a pending Confirm.
                // A complete request, then quieter room words: end with the request as it was.
                turn.keptText = kept
                turn.endNote = " (room words after the request dropped)"
                return .endOfSpeech
            } else if let end = turn.endOfSpeech,
                      TurnEndpointer.shouldEnd(text: turn.lastText, lastTextAt: end,
                                               lastVoiceAt: tap.lastVoicedAt, now: now) {
                // Not on partial-text silence alone: partials lag the audio and people pause
                // after "the last, uh" (owner, 02:18). Voiced audio and a trailing word hold it open.
                let wait = TurnEndpointer.silence(after: turn.lastText)
                turn.endNote = wait > TurnEndpointer.fast ? " (trailed off: waited \(wait) s)" : ""
                return .endOfSpeech
            }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    /// Gives the tail gate each finished 100 ms block since the last poll.
    private func feedTail(_ turn: Turn, now: Date) {
        let blocks = LevelBlocks.blocks(tap.levelSamples(since: turn.levelFrom), from: turn.levelFrom, to: now)
        let done = max(0, blocks.count - 1)  // the newest block is still filling
        guard done > turn.fedBlocks else { return }
        for i in turn.fedBlocks..<done {
            turn.tail.level(blocks[i], at: turn.levelFrom.addingTimeInterval(Double(i + 1) * LevelBlocks.width))
        }
        turn.fedBlocks = done
    }

    /// Gives the barge echo gate each finished 100 ms block since the barge turn started: mic
    /// blocks from the tap, the reply's playback RMS from AudioOut's ring (a block with no
    /// buffer is 0, so playback gaps can never raise the coupling).
    private func feedGate(_ barge: BargeRun, now: Date) {
        guard let from = barge.gateThrough else { return }
        let mic = LevelBlocks.blocks(tap.levelSamples(since: from), from: from, to: now)
        let out = LevelBlocks.blocks(barge.outLevels(), from: from, to: now)
        let done = min(max(0, mic.count - 1), max(0, out.count - 1))  // the newest block is still filling
        guard done > barge.fedBlocks else { return }
        for i in barge.fedBlocks..<done {
            barge.gate.block(mic: mic[i], out: out[i],
                             at: from.addingTimeInterval(Double(i + 1) * LevelBlocks.width))
        }
        barge.fedBlocks = done
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
        turn.path = "SpeechAnalyzer"
        let transcriber = SpeechTranscriber(
            locale: Locale(identifier: "en-US"),
            transcriptionOptions: [],
            reportingOptions: [.volatileResults],
            attributeOptions: [.transcriptionConfidence]
        )
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                try await request.downloadAndInstall()
            }
        } catch {
            turn.path = "SpeechAnalyzer assets failed (\(error.localizedDescription))"
            return false
        }
        guard let target = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]),
              let converter = AVAudioConverter(from: format, to: target)
        else {
            turn.path = "SpeechAnalyzer: no audio format/converter from \(format)"
            return false
        }
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        do {
            try await analyzer.start(inputSequence: stream)
        } catch {
            continuation.finish()
            turn.path = "SpeechAnalyzer start failed (\(error.localizedDescription))"
            return false
        }
        let collected = TranscriptBox()
        let stats = turn.stats
        let confidence = turn.confidence
        let resultsTask = Task {
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters)
                    collected.update(text, isFinal: result.isFinal)
                    if result.isFinal {
                        for run in result.text.runs {
                            if let c = run[AttributeScopes.SpeechAttributes.ConfidenceAttribute.self] {
                                confidence.add(c, weight: result.text[run.range].characters.count)
                            }
                        }
                    }
                    stats.result()
                    partial(collected.text)
                }
            } catch {
                stats.fail(error.localizedDescription)
            }
        }
        turn.cancelHandler = {
            continuation.finish()
            resultsTask.cancel()
            Task { await analyzer.cancelAndFinishNow() }
        }
        // shortcut: the converter is used from the audio thread only after the ring
        // snapshot below has been fed, so it stays single-threaded.
        let feed: @Sendable (AVAudioPCMBuffer, Date) -> Void = { buffer, _ in
            let out = Self.convert(converter: converter, from: buffer, to: target)
            stats.fed(converted: out.map { Int($0.frameLength) })
            if let out {
                continuation.yield(AnalyzerInput(buffer: out))
            }
        }
        if !turn.followUp {
            for chunk in tap.ringSnapshot() { feed(chunk.buffer, chunk.at) }
        } else {
            // Follow-up: replay only chunks stamped at/after replayStart; replayStart >= the
            // drain time already guarantees no chunk recorded while audio was busy is replayed.
            let snapshot = tap.ringSnapshot()
            let playable = TurnEndpointer.replayable(stamps: snapshot.map { $0.at },
                                                     drainedAt: turn.replayFrom)
            for (chunk, ok) in zip(snapshot, playable) where ok { feed(chunk.buffer, chunk.at) }
        }
        tap.setSink(feed)
        let verdict = await watch(turn: turn)
        guard !turn.cancelled, !turn.finished else { return true }
        tap.setSink(nil)
        turn.path += ", ended by \(verdict)"
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
        turn.path = turn.path.isEmpty ? "SFSpeechRecognizer" : "SFSpeechRecognizer after \(turn.path)"
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.isAvailable
        else {
            turn.path += " (recognizer unavailable)"
            completeTurn(turn, nothing: true)
            return
        }
        let stats = turn.stats
        let confidence = turn.confidence
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        let task = recognizer.recognitionTask(with: request) { result, error in
            if let result {
                stats.result()
                let text = result.bestTranscription.formattedString
                partial(text)
                if result.isFinal {
                    // Segment confidence is 0 until final; 0 everywhere means "not reported".
                    for seg in result.bestTranscription.segments where seg.confidence > 0 {
                        confidence.add(Double(seg.confidence), weight: seg.substring.count)
                    }
                    finalize(text)
                }
            } else if let error {
                stats.fail(error.localizedDescription)
                nothing()
            }
        }
        turn.cancelHandler = { task.cancel() }
        for chunk in tap.ringSnapshot() where !turn.followUp {
            request.append(chunk.buffer)
        }
        if turn.followUp {
            // Follow-up: replay only chunks stamped at/after replayStart; replayStart >= the
            // drain time already guarantees no chunk recorded while audio was busy is replayed.
            let snapshot = tap.ringSnapshot()
            let playable = TurnEndpointer.replayable(stamps: snapshot.map { $0.at },
                                                     drainedAt: turn.replayFrom)
            for (chunk, ok) in zip(snapshot, playable) where ok { request.append(chunk.buffer) }
        }
        tap.setSink { [weak request] buffer, _ in
            stats.fed(converted: Int(buffer.frameLength))
            request?.append(buffer)
        }
        Task { @MainActor [weak self] in
            guard let self else { return }
            let verdict = await self.watch(turn: turn)
            guard !turn.cancelled, !turn.finished else { return }
            self.tap.setSink(nil)
            turn.path += ", ended by \(verdict)"
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
        // The converter calls this block synchronously inside convert(), never concurrently.
        nonisolated(unsafe) var consumed = false
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

// MARK: - per-turn STT counters (debug page)

/// Fed from the audio thread, read on main when a turn ends empty.
private final class FeedStats: @unchecked Sendable {
    private let lock = NSLock()
    private var buffers = 0
    private var dropped = 0
    private var frames = 0
    private var results = 0
    private var error: String?

    func fed(converted: Int?) {
        lock.lock(); defer { lock.unlock() }
        buffers += 1
        if let converted { frames += converted } else { dropped += 1 }
    }

    func result() {
        lock.lock(); defer { lock.unlock() }
        results += 1
    }

    func fail(_ message: String) {
        lock.lock(); defer { lock.unlock() }
        error = message
    }

    var summary: String {
        lock.lock(); defer { lock.unlock() }
        var out = "mic buffers \(buffers) (\(dropped) failed to convert), frames \(frames), STT results \(results)"
        if let error { out += ", STT error: \(error)" }
        return out
    }
}

// MARK: - recogniser confidence

/// Mean confidence of the final words, weighted by characters; nil when the recogniser gave none.
private final class ConfidenceBox: @unchecked Sendable {
    private let lock = NSLock()
    private var sum = 0.0
    private var weight = 0

    func add(_ value: Double, weight w: Int) {
        guard w > 0, value.isFinite else { return }
        lock.lock(); defer { lock.unlock() }
        sum += min(1, max(0, value)) * Double(w)
        weight += w
    }

    var mean: Double? {
        lock.lock(); defer { lock.unlock() }
        return weight > 0 ? sum / Double(weight) : nil
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

// MARK: - barge-in run state

/// One barge listening pass, shared by AppController (which starts it and appends the reply's
/// spoken texts) and SpeechInput's barge turn (which feeds the gate and confirms on a partial).
/// Main-actor only, like the turns that hold it.
final class BargeRun {
    /// The reply's spoken texts so far (every .audio/.say of the turn); AppController appends.
    var reply: [String] = []
    /// The echo gate; its margin is AEC's only when voice processing is really on.
    var gate: EchoGate
    /// Barge-in wanted (health barge_in); the turn is only started when true.
    let enabled: Bool
    /// AEC really on (the start() probe stuck): reason .aec and the AEC margin only then.
    let aec: Bool
    /// The reply's playback RMS (AudioOut's level ring), read on the main actor.
    let outLevels: () -> [LevelSample]
    /// Gate blocks already fed (each finished 100 ms block reaches the gate once).
    var fedBlocks = 0
    /// Set when the barge turn starts: gate blocks before it are pre-reply audio, never fed.
    var gateThrough: Date?
    /// True once a barge-in fired; the turn then runs on as an ordinary follow-up.
    var confirmed = false
    /// AppController's stop-the-reply path (audio.stop, stream cancel, orb).
    var onConfirmed: ((BargeReason) -> Void)?
    /// Words of the partial heard while the gate was quiet (the reply's echo): the word check
    /// starts after them.
    var freshFrom = 0
    /// Partials seen, and word checks made while the gate was a candidate (debug note only).
    var partials = 0, checked = 0
    /// Told once when the run ends (confirmed, cancelled at the drain, or the recogniser ended):
    /// the gate's numbers for the one-shot `barge_gate` debug note.
    var onEnded: ((String) -> Void)?
    private var ended = false

    func end() {
        guard !ended else { return }
        ended = true
        onEnded?("\(gate.summary) partials=\(partials) checked=\(checked) confirmed=\(confirmed ? 1 : 0)")
    }

    init(enabled: Bool, aec: Bool, outLevels: @escaping () -> [LevelSample]) {
        self.enabled = enabled
        self.aec = aec
        self.outLevels = outLevels
        self.gate = EchoGate(margin: aec ? EchoGate.aecMargin : EchoGate.defaultMargin)
    }
}

// MARK: - mic tap

/// Tap buffers are only valid inside the callback, so everything is copied.
/// State is lock-guarded: the tap callback runs on the audio thread while
/// sink swaps and ring snapshots happen on main.
private final class MicTap: @unchecked Sendable {
    private let lock = NSLock()
    private var ring: [(buffer: AVAudioPCMBuffer, at: Date)] = []
    private var sink: (@Sendable (AVAudioPCMBuffer, Date) -> Void)?
    private var vad = VoiceActivity()
    private var voicedAt: Date?
    private var levels: [LevelSample] = []  // the last 40 s of buffer levels (turn gates read them)

    /// Last mic buffer clearly above the noise floor (the user is still talking).
    var lastVoicedAt: Date? {
        lock.lock()
        defer { lock.unlock() }
        return voicedAt
    }

    func install(on node: AVAudioInputNode, format: AVAudioFormat) {
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            let copy = Self.copy(buffer)
            let now = Date()
            var sink: (@Sendable (AVAudioPCMBuffer, Date) -> Void)?
            let rms = Self.rms(copy)
            lock.lock()
            if vad.isVoiced(rms: rms) { voicedAt = now }
            levels.append(LevelSample(at: now, rms: rms, frames: Int(copy.frameLength)))
            if let first = levels.first, now.timeIntervalSince(first.at) > 41 {
                levels.removeAll { now.timeIntervalSince($0.at) > 40 }
            }
            ring.append((copy, now))
            ring.removeAll { now.timeIntervalSince($0.at) > 2 }
            sink = self.sink
            lock.unlock()
            sink?(copy, now)
        }
    }

    func setSink(_ newSink: (@Sendable (AVAudioPCMBuffer, Date) -> Void)?) {
        lock.lock()
        sink = newSink
        lock.unlock()
    }

    func levelSamples(since: Date = .distantPast) -> [LevelSample] {
        lock.lock()
        defer { lock.unlock() }
        guard let i = levels.firstIndex(where: { $0.at >= since }) else { return [] }
        return Array(levels[i...])
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
