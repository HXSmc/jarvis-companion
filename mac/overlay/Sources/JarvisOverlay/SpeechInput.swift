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
    /// Read-only view of `engine`: the AEC path (barge_mode aec) attaches AudioOut's player to
    /// it before start(), so the mic's voice processing also cancels the reply audio.
    var audioEngine: AVAudioEngine { engine }
    private let tap = MicTap()
    private var isRunning = false
    /// startEngine succeeded once: from then on a stopped engine is a fault to rebuild.
    private var startedOnce = false
    /// The tap's channel pick waits while a barge run exists (a reply plays): the other node channels
    /// may be raw mics or the speaker reference, never moved to over Jarvis's own voice.
    private var currentTurn: Turn? { didSet { tap.setHold(currentTurn?.barge != nil) } }
    private var turnTask: Task<Void, Never>?
    /// How the last finished turn was heard (STT path, counters, what ended it): debug page only.
    private(set) var lastDiagnosis = ""
    private let log = Logger(subsystem: "JarvisOverlay", category: "SpeechInput")
    /// TailGate's ratio; the service sends it on /overlay/health (OVERLAY_TAIL_LEVEL_RATIO).
    var tailRatio = TailGate.defaultRatio
    /// Voice processing (Apple's echo canceller) is on, read back from the input node after the
    /// last reconfigure; the gate uses the AEC margin and the speakers rule only when true.
    private(set) var aecActive = false
    private var tapInstalled = false
    /// The mono format the tap hands on (levels, ring, recogniser): channel 0 of the node's format.
    private var micFormat: AVAudioFormat?
    private var configObserver: NSObjectProtocol?
    /// The engine stopped by itself (AVAudioEngineConfigurationChange: a device or route change);
    /// AppController reconfigures it at once.
    var onConfigurationChange: (() -> Void)?
    /// Voice processing on and channel 0 was exact digital silence for 1 s while channel `to` was live:
    /// the tap now hands on channel `to` (the `barge_aec src_switch` row).
    var onSourceSwitch: ((_ to: Int) -> Void)?
    /// One per finished turn while voice processing is on: the `mic_signal` row and the no-signal guard.
    var onTurnSignal: ((TurnSignal) -> Void)?
    /// The input node's channel count as tapped (the `src=` field).
    private var nodeChannels = 1

    /// A finished turn's mic signal (numbers and fixed words only, never audio or words).
    struct TurnSignal {
        var kind: String          // wake|followup|barge
        var heardNothing: Bool    // it ended through onNothing (no_speech on a wake turn)
        var sttResults: Int
        var peakDb: Double        // the handed-on channel's peak over the turn, dBFS
        var detail: String        // the mic_signal row
    }

    /// What the service's follow-up gate reads with the words: 100 ms mic levels, STT confidence.
    struct Heard {
        var levels: [Double]
        var conf: Double?
        var audioFrom: Date? = nil  // capture time of the first audio the recogniser was fed
    }

    init() {}

    // MARK: mic

    /// What one reconfigure did, for the `barge_aec` debug row (numbers and fixed words only).
    struct EngineSetup {
        var active: Bool       // voice processing on, read back from the input node
        var failed: String?    // enable|format|start: turning it on failed, the plain path runs
        var rate: Double       // the input node's real format, as tapped
        var channels: Int
        var src: String        // ch0|mono: what levels, the ring and the recogniser hear
        var outRate: Double    // mixer -> output connection while voice processing is on (0 when off)
        var outChannels: Int
        var ms: Int
    }

    private enum EngineFail: Error { case enable, format, start }
    /// The NSError code of the last failed engine.start() (Mac run b: -10875), for the failed row.
    private var startErr: Int?
    /// The mixer -> output connection carries an explicit voice-processing format; the plain path
    /// gives it back to the hardware format.
    private var outputPinned = false

    /// start() succeeded once, but the engine is not running now: a configuration change stopped it,
    /// or a reconfigure threw (isRunning is false then), so the next change notification retries.
    var engineStoppedOnItsOwn: Bool { EchoCancel.engineStopped(startedOnce: startedOnce, running: engine.isRunning) }

    /// The loudest buffer RMS per node channel since the tap was installed (the `barge_aec levels` row
    /// shows whether channel 0 really carries the voice).
    var channelPeaks: [Double] { tap.channelPeaks }

    /// What levels, the ring and the recogniser hear: `mono` (1-channel node) or `ch<n>`.
    var src: String { EchoCancel.monoSource(channels: nodeChannels, channel: tap.sourceChannel) }

    /// An unconfirmed barge run is listening (the reply plays on).
    var bargeListening: Bool {
        guard let turn = currentTurn, let barge = turn.barge, !turn.finished, !turn.cancelled else { return false }
        return !barge.confirmed
    }

    /// A confirmed barge-in's turn is still listening to the owner.
    var bargeTurnOpen: Bool {
        guard let turn = currentTurn, let barge = turn.barge, !turn.finished, !turn.cancelled else { return false }
        return barge.confirmed
    }

    /// The one path that (re)starts the always-on input tap and ring buffer: launch, a route or
    /// mode change, a configuration change. `attach(on)` moves AudioOut's player while the engine
    /// is stopped (on: onto this engine, so the canceller hears the reply). A failure turning voice
    /// processing on falls back to the plain path (`failed` says why). Throws when even the plain
    /// path has no usable input device (or mic permission was never granted).
    /// shortcut: runs on the main actor, so a voice-processing start can hold the UI 1-3 s (the
    /// row's start_ms measures it); move it off the main actor if the live run shows a freeze.
    func reconfigure(voiceProcessing want: Bool, attach: (Bool) -> Void) throws -> EngineSetup {
        let started = Date()
        observeConfigurationChanges()
        if engineStoppedOnItsOwn, let turn = currentTurn {
            let tell = (turn.barge?.confirmed ?? true) ? turn.onNothing : nil
            cancel()
            tell?("audio device changed")
        }
        aecActive = false
        startErr = nil
        var failed: String?
        var format: AVAudioFormat
        if want {
            do { format = try startEngine(voiceProcessing: true, attach: attach) }
            catch {
                // The node's formats as voice processing left them, for the row (the live run needs numbers).
                let seen = engine.inputNode.outputFormat(forBus: 0)
                let out = engine.outputNode.inputFormat(forBus: 0)
                failed = "\((error as? EngineFail) ?? .start) rate=\(Int(seen.sampleRate)) ch=\(seen.channelCount)"
                    + " out_rate=\(Int(out.sampleRate)) out_ch=\(out.channelCount)" + (startErr.map { " err=\($0)" } ?? "")
                format = try startPlain(attach: attach)
            }
        } else {
            format = try startPlain(attach: attach)
        }
        aecActive = want && failed == nil && engine.inputNode.isVoiceProcessingEnabled
        let out = aecActive ? engine.outputNode.inputFormat(forBus: 0) : nil
        return EngineSetup(active: aecActive, failed: failed, rate: format.sampleRate,
                           channels: Int(format.channelCount),
                           src: EchoCancel.monoSource(channels: Int(format.channelCount)),  // printed only when on
                           outRate: out?.sampleRate ?? 0, outChannels: Int(out?.channelCount ?? 0),
                           ms: Int(Date().timeIntervalSince(started) * 1000))
    }

    private func startPlain(attach: (Bool) -> Void) throws -> AVAudioFormat {
        do { return try startEngine(voiceProcessing: false, attach: attach) } catch {
            throw NSError(domain: "JarvisOverlay.SpeechInput", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "No audio input available"])
        }
    }

    /// Stop, set voice processing, THEN read the input format (voice processing changes its rate
    /// and channels), tap it, attach the player, start.
    private func startEngine(voiceProcessing on: Bool, attach: (Bool) -> Void) throws -> AVAudioFormat {
        if engine.isRunning { engine.stop() }
        isRunning = false
        let input = engine.inputNode
        if tapInstalled { input.removeTap(onBus: 0); tapInstalled = false }
        if input.isVoiceProcessingEnabled != on {
            do { try input.setVoiceProcessingEnabled(on) } catch { throw EngineFail.enable }
        }
        if on {
            guard input.isVoiceProcessingEnabled else { throw EngineFail.enable }
            input.isVoiceProcessingAGCEnabled = false  // AGC pumps the level between words; the gates read levels
            // The owner's music: the least ducking Apple allows, and never more while he speaks.
            input.voiceProcessingOtherAudioDuckingConfiguration = .init(enableAdvancedDucking: false, duckingLevel: .min)
        }
        let format = input.outputFormat(forBus: 0)
        guard EchoCancel.formatUsable(rate: format.sampleRate, channels: Int(format.channelCount)),
              let mono = on ? MicTap.monoFormat(of: format) : format else { throw EngineFail.format }
        if on {
            // The voice-processing unit needs the same client rate on input and output (err=-10875
            // otherwise). Left unset, the mixer -> output connection tracks the output node (44100 Hz
            // on the MacBook Air); once set, the client keeps it right after every rebuild (Apple's
            // mainMixerNode docs), which is this path.
            let out = engine.outputNode.inputFormat(forBus: 0)
            guard let pick = EchoCancel.voiceOutputFormat(inputRate: format.sampleRate,
                                                          inputChannels: Int(format.channelCount),
                                                          outputChannels: Int(out.channelCount)),
                  let pinned = AVAudioFormat(standardFormatWithSampleRate: pick.rate,
                                             channels: AVAudioChannelCount(pick.channels)) else { throw EngineFail.format }
            engine.connect(engine.mainMixerNode, to: engine.outputNode, format: pinned)
            outputPinned = true
        } else if outputPinned {
            // Plain path: back to the hardware output format, as the engine tracked it before.
            let hw = engine.outputNode.outputFormat(forBus: 0)
            let plain = hw.sampleRate > 0 && hw.channelCount > 0
                ? AVAudioFormat(standardFormatWithSampleRate: hw.sampleRate, channels: hw.channelCount) : nil
            engine.connect(engine.mainMixerNode, to: engine.outputNode, format: plain)
            outputPinned = false
        }
        tap.clearRing()  // ring audio in the old format must never be replayed into a new-format turn
        // The tap keeps the node's own format (the input node does no format conversion on a device,
        // Apple's AVAudioInputNode docs). Voice processing on: it hands on channel 0 only, in `mono`;
        // off: every channel, as before.
        tap.install(on: input, format: format, mono: mono) { [weak self] to in
            Task { @MainActor in self?.onSourceSwitch?(to) }
        }
        micFormat = mono
        nodeChannels = Int(format.channelCount)
        tapInstalled = true
        attach(on)
        engine.prepare()
        do { try engine.start() } catch { startErr = (error as NSError).code; throw EngineFail.start }
        isRunning = true
        startedOnce = true
        return format
    }

    /// The engine posts this when a device or route change stopped it (mic and replies both).
    private func observeConfigurationChanges() {
        guard configObserver == nil else { return }
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.onConfigurationChange?() }
        }
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
        let format = micFormat ?? engine.inputNode.outputFormat(forBus: 0)
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

    /// The owner's open turn, for the wake gate (WakeGate): nil when there is none or it is a barge run.
    /// heard = its words so far (the newest partial).
    var openTurn: (followUp: Bool, hasSpeech: Bool, heard: String)? {
        guard let turn = currentTurn, turn.barge == nil, !turn.finished, !turn.cancelled else { return nil }
        return (turn.followUp, turn.hasSpeech, turn.lastText)
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
        if let barge = turn.barge, !barge.confirmed { checkBarge(barge, turn: turn, partial: text, now: Date(), recheck: false) }
        turn.tail.partial(text, at: Date())
        turn.onPartial?(text)
    }

    /// The barge-in check on one partial (`recheck`: the last partial again after new over blocks,
    /// C2): `BargeCheck` decides from the gate's blocks and the partial's word flags; a decisive
    /// partial (a name or stop word in its fresh words) gets one `barge_check` row. On confirm the
    /// turn goes on as an ordinary follow-up.
    private func checkBarge(_ barge: BargeRun, turn: Turn, partial: String, now: Date, recheck: Bool) {
        feedGate(barge, now: now)  // the gate is current before it is read
        if !recheck { barge.partials += 1 }
        let reply = barge.reply
        let count = BargeWords.wordCount(partial)
        let level = BargeCheck.Level(mic: barge.micBlocks.last ?? 0, out: barge.outBlocks.last ?? 0,
                                     coupling: barge.gate.coupling, ratio: barge.lastRatio)
        barge.freshFrom = barge.tracker.expire(at: now, freshFrom: barge.freshFrom, wordCount: count)
        func judge(over: Bool, candidate: Bool) -> BargeCheck.Verdict {
            let (verdict, row) = barge.tracker.judge(
                at: now, recheck: recheck, recentlyOver: over, candidate: candidate,
                freshFrom: barge.freshFrom, wordCount: count,
                words: BargeWords.check(partial: partial, reply: reply, freshFrom: barge.freshFrom,
                                       speakers: barge.aec), level: level)
            if let row { barge.onCheck?(row) }
            return verdict
        }
        guard barge.gate.recentlyOver(at: now) else {
            // Echo only so far: these words are the reply's, the owner's can only come after them.
            // C1: on an echo-cancelled run a wake name or stop word the reply lacks stays fresh: its
            // partial trails the audio, its over blocks may reach the gate after it (re-check, C2).
            if let index = barge.tracker.keep(at: now, freshFrom: barge.freshFrom, wordCount: count,
                                              commandIndex: { BargeWords.commandIndex(partial: partial, reply: reply, from: $0) }) {
                barge.freshFrom = index
            } else {
                barge.freshFrom = BargeWords.wordCount(partial)
            }
            _ = judge(over: false, candidate: false)
            return
        }
        // The tracker adds the echo-cancelled hold memory (BargeCheck.aecHoldMemory, row 20:59:22).
        let candidate = barge.gate.candidate(at: now) || barge.tracker.candidate(at: now)
        if candidate {
            barge.maxNew = max(barge.maxNew, BargeWords.newWordCount(partial: partial, reply: reply,
                                                                      freshFrom: barge.freshFrom))
        }
        // Through the echo canceller (speakers) the fresh words must name Jarvis or say stop (hub Q5).
        guard let rule = judge(over: true, candidate: candidate).rule, barge.enabled else { return }
        barge.rule = BargeRule(rawValue: rule.rawValue)
        barge.confirmed = true
        turn.capFrom = now
        // The tail gate and the sent levels start at the confirmation, not over the reply's echo.
        turn.tail = TailGate(ratio: tailRatio)
        turn.fedBlocks = 0
        turn.levelFrom = Date()
        barge.onConfirmed?(barge.aec ? .aec : .level)
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
            reportSignal(turn, heardNothing: true)
            turn.onNothing?(turn.diagnosis)
            return
        }
        let stripped = Self.stripWake(raw, followUp: turn.followUp)
        guard !stripped.isEmpty else {
            reportSignal(turn, heardNothing: true)
            turn.onNothing?("only the wake word was heard; \(turn.diagnosis)")
            return
        }
        reportSignal(turn, heardNothing: false)
        // A barge turn keeps its wake words: the speakers rule (bargeTurnText) needs the name, and
        // freshFrom counts words of the unstripped text. The service drops the wake itself.
        let text = turn.barge != nil ? raw.trimmingCharacters(in: .whitespacesAndNewlines) : stripped
        log.debug("final \(text, privacy: .private)")
        let levels = LevelBlocks.blocks(tap.levelSamples(), from: turn.levelFrom, to: end.addingTimeInterval(0.3))
        turn.onFinal?(text, Date().timeIntervalSince(end) * 1000, end,
                      Heard(levels: Array(levels.suffix(LevelBlocks.maxSent)), conf: turn.confidence.mean,
                            audioFrom: turn.stats.firstAt))
    }

    /// Voice processing on: one `mic_signal` row for the finished turn, over the audio the recogniser
    /// heard (from `levelFrom`, so a wake turn's replayed ring with "Hey Jarvis" counts).
    private func reportSignal(_ turn: Turn, heardNothing: Bool) {
        guard aecActive, let report = onTurnSignal else { return }
        let signal = tap.signal(since: turn.levelFrom)
        let kind = turn.barge != nil ? "barge" : turn.followUp ? "followup" : "wake"
        let results = turn.stats.resultCount
        report(TurnSignal(kind: kind, heardNothing: heardNothing, sttResults: results,
                          peakDb: signal.peakDb(tap.sourceChannel),
                          detail: "kind=\(kind) src=\(src) \(signal.detail) stt_results=\(results)"))
    }

    /// Drops a leading wake phrase (a follow-up: only at the very start), and stray punctuation.
    static func stripWake(_ raw: String, followUp: Bool = false) -> String {
        TurnEndpointer.stripWake(raw, followUp: followUp)
    }

    // MARK: watchdog

    private enum Verdict { case keepWaiting, endOfSpeech, nothing, timedOut, cancelled }

    /// Polls turn state every 50 ms until an end condition fires.
    private func watch(turn: Turn) async -> Verdict {
        while true {
            if turn.cancelled || turn.finished { return .cancelled }
            // Barge listening runs while the reply plays: silence, the caps and the endpointer
            // never end it — only a confirmed barge-in or a cancel (the reply drain) does.
            if let barge = turn.barge, !barge.confirmed {
                let now = Date()
                feedGate(barge, now: now)
                // C2 (BargeCheck.Tracker.wantsRecheck): over blocks that came after the last partial: its words are checked again.
                if barge.tracker.wantsRecheck(at: now), !turn.lastText.isEmpty {
                    checkBarge(barge, turn: turn, partial: turn.lastText, now: now, recheck: true)
                }
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
                      let rule = TurnEndpointer.endRule(text: turn.lastText, lastTextAt: end,
                                                        lastVoiceAt: tap.lastVoicedAt, now: now) {
                // Not on partial-text silence alone: partials lag the audio and people pause
                // after "the last, uh" (owner, 02:18). Voiced audio and a trailing word hold it open.
                // The note names the rule and the gaps (numbers only), so a cut request says why.
                let voiceGap = tap.lastVoicedAt.map { String(format: "%.2f", now.timeIntervalSince($0)) } ?? "none"
                let words = TurnEndpointer.stripWake(turn.lastText).split(separator: " ").count
                let textGap = String(format: "%.2f", now.timeIntervalSince(end))
                turn.endNote = " (rule \(rule): text quiet \(textGap) s, voice quiet \(voiceGap) s, words \(words))"
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
            let at = from.addingTimeInterval(Double(i + 1) * LevelBlocks.width)
            let overBefore = barge.gate.overTotal
            let echo = barge.gate.echoEstimate(nextOut: out[i])
            barge.gate.block(mic: mic[i], out: out[i], at: at)
            barge.tracker.block(over: barge.gate.overTotal > overBefore, at: at)
            barge.lastRatio = echo > 0 ? mic[i] / echo : 0
            barge.micBlocks.append(mic[i])
            barge.outBlocks.append(out[i])
            // The first over block of the newest candidate run: voice_to_stop_ms counts from it.
            if barge.gate.isCandidate { barge.voiceFrom = barge.gate.candidateSince }
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
        let feed: @Sendable (AVAudioPCMBuffer, Date) -> Void = { buffer, at in
            stats.stamp(at)
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
            stats.stamp(chunk.at)
            request.append(chunk.buffer)
        }
        if turn.followUp {
            // Follow-up: replay only chunks stamped at/after replayStart; replayStart >= the
            // drain time already guarantees no chunk recorded while audio was busy is replayed.
            let snapshot = tap.ringSnapshot()
            let playable = TurnEndpointer.replayable(stamps: snapshot.map { $0.at },
                                                     drainedAt: turn.replayFrom)
            for (chunk, ok) in zip(snapshot, playable) where ok {
                stats.stamp(chunk.at)
                request.append(chunk.buffer)
            }
        }
        tap.setSink { [weak request] buffer, at in
            stats.stamp(at)
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
    private var first: Date?

    /// Capture time of the first chunk fed (follow-up echo timing, BargeWords.followUpText).
    func stamp(_ at: Date) {
        lock.lock(); defer { lock.unlock() }
        if first == nil { first = at }
    }

    var firstAt: Date? {
        lock.lock(); defer { lock.unlock() }
        return first
    }

    func fed(converted: Int?) {
        lock.lock(); defer { lock.unlock() }
        buffers += 1
        if let converted { frames += converted } else { dropped += 1 }
    }

    func result() {
        lock.lock(); defer { lock.unlock() }
        results += 1
    }

    var resultCount: Int {
        lock.lock(); defer { lock.unlock() }
        return results
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
    /// When AppController made this run: a restart measures the run's age from here.
    let createdAt = Date()
    /// AppController queued this run's one delayed restart (it ended younger than minBargeRunS).
    var retryQueued = false
    /// True once a barge-in fired; the turn then runs on as an ordinary follow-up.
    var confirmed = false
    /// AppController's stop-the-reply path (audio.stop, stream cancel, orb).
    var onConfirmed: ((BargeReason) -> Void)?
    /// The barge-in decision state (BargeCheck): words of the partial heard while the gate was
    /// quiet are the reply's echo and the word check starts after them (`freshFrom`).
    var tracker: BargeCheck.Tracker
    var freshFrom = 0
    /// The newest fed block's mic over the gate's echo estimate (the `barge_check` row's ratio).
    var lastRatio = 0.0
    /// One `barge_check` row (numbers and fixed words only).
    var onCheck: ((String) -> Void)?
    /// Partials seen (debug note only; the checks are counted in `tracker`).
    var partials = 0
    /// Word checks made while the gate was a candidate (re-checks included).
    var checked: Int { tracker.checked }
    /// Most new (not-in-the-reply) words any checked partial held; the word check needs 2.
    var maxNew = 0
    /// Which word rule confirmed (name|stop|words), for the `barge_gate`/`barge_in` notes.
    var rule: BargeRule?
    /// End of the first over-echo block of the candidate run that confirmed.
    var voiceFrom: Date?
    /// Every fed 100 ms block's mic and out RMS (the note's p90s; a run lasts one reply).
    var micBlocks: [Double] = [], outBlocks: [Double] = []
    /// Told once when the run ends (confirmed, cancelled at the drain, or the recogniser ended):
    /// the gate's numbers for the one-shot `barge_gate` debug note.
    var onEnded: ((String) -> Void)?
    private var ended = false

    func end() {
        guard !ended else { return }
        ended = true
        // skip_over / skip_hold: checks turned down before the word rule (gate quiet / no 3-in-a-row).
        onEnded?("\(gate.summary) partials=\(partials) checked=\(checked) max_new=\(maxNew) "
                 + "rule=\(rule?.rawValue ?? "none") aec=\(aec ? 1 : 0) mic_p90=\(Self.p90(micBlocks)) "
                 + "out_p90=\(Self.p90(outBlocks)) confirmed=\(confirmed ? 1 : 0) "
                 + "skip_over=\(tracker.skipNotOver) skip_hold=\(tracker.skipNoHold)"
                 + (tracker.capped > 0 ? " capped=\(tracker.capped)" : ""))
    }

    /// The 90th percentile, 3 decimals ("0.000" for no blocks).
    static func p90(_ values: [Double]) -> String {
        let sorted = values.sorted()
        let v = sorted.isEmpty ? 0 : sorted[min(sorted.count - 1, Int(Double(sorted.count) * 0.9))]
        return String(format: "%.3f", v)
    }

    /// `coupling`: a run restarted in the same reply starts at the ended run's (C4).
    init(enabled: Bool, aec: Bool, coupling: Double? = nil, outLevels: @escaping () -> [LevelSample]) {
        self.enabled = enabled
        self.aec = aec
        self.outLevels = outLevels
        let margin = aec ? EchoGate.aecMargin : EchoGate.defaultMargin
        self.gate = coupling.map { EchoGate(margin: margin, coupling: $0) } ?? EchoGate(margin: margin)
        self.tracker = BargeCheck.Tracker(aec: aec)
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
    private var peaks: [Double] = []  // loudest buffer RMS per node channel since install
    private var signals: [(at: Date, signal: EchoCancel.MicSignal)] = []  // per buffer, same 40 s as levels
    private var pick = EchoCancel.SourcePick()
    private var hold = false

    var sourceChannel: Int {
        lock.lock()
        defer { lock.unlock() }
        return pick.channel
    }

    func setHold(_ on: Bool) {
        lock.lock()
        hold = on
        lock.unlock()
    }

    /// The per-channel signal of every buffer since `since`, summed.
    func signal(since: Date) -> EchoCancel.MicSignal {
        lock.lock()
        defer { lock.unlock() }
        var out = EchoCancel.MicSignal()
        for s in signals where s.at >= since {
            out.add(peak: s.signal.peak, sumSq: s.signal.sumSq, frames: s.signal.frames, ch0Zero: s.signal.ch0Zero)
        }
        return out
    }

    /// Mono, deinterleaved, same sample type and rate as the node: what the tap hands on.
    static func monoFormat(of format: AVAudioFormat) -> AVAudioFormat? {
        AVAudioFormat(commonFormat: format.commonFormat, sampleRate: format.sampleRate,
                      channels: 1, interleaved: false)
    }

    var channelPeaks: [Double] {
        lock.lock()
        defer { lock.unlock() }
        return peaks
    }

    /// Last mic buffer clearly above the noise floor (the user is still talking).
    var lastVoicedAt: Date? {
        lock.lock()
        defer { lock.unlock() }
        return voicedAt
    }

    /// `onSwitch(to)`: the voice-processing path moved off an exact-silent channel 0 (SourcePick).
    func install(on node: AVAudioInputNode, format: AVAudioFormat, mono: AVAudioFormat,
                 onSwitch: @escaping @Sendable (Int) -> Void) {
        lock.lock()
        peaks = []
        pick = EchoCancel.SourcePick()  // every new tap starts at channel 0
        lock.unlock()
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            let signal = Self.signal(buffer)
            let frames = Int(buffer.frameLength)
            let perChannel = signal.peak.indices.map { signal.rms($0) }
            let picking = mono.channelCount < buffer.format.channelCount
            lock.lock()
            let moved = picking ? pick.step(ch0Zero: frames > 0 && signal.ch0Zero && !signal.peak.isEmpty,
                                            rmsDb: perChannel.map { EchoCancel.dbfs($0) },
                                            seconds: Double(frames) / max(1, buffer.format.sampleRate),
                                            hold: hold) : nil
            let channel = pick.channel
            lock.unlock()
            let copy = picking ? Self.channel(channel, of: buffer, mono: mono) : Self.copy(buffer)
            let now = Date()
            var sink: (@Sendable (AVAudioPCMBuffer, Date) -> Void)?
            let rms = Self.rms(copy)
            lock.lock()
            if peaks.count != perChannel.count { peaks = perChannel } else {
                for i in peaks.indices { peaks[i] = max(peaks[i], perChannel[i]) }
            }
            if vad.isVoiced(rms: rms) { voicedAt = now }
            levels.append(LevelSample(at: now, rms: rms, frames: Int(copy.frameLength)))
            signals.append((now, signal))
            if let first = levels.first, now.timeIntervalSince(first.at) > 41 {
                levels.removeAll { now.timeIntervalSince($0.at) > 40 }
                signals.removeAll { now.timeIntervalSince($0.at) > 40 }
            }
            ring.append((copy, now))
            ring.removeAll { now.timeIntervalSince($0.at) > 2 }
            sink = self.sink
            lock.unlock()
            sink?(copy, now)
            if let moved { onSwitch(moved) }
        }
    }

    func clearRing() {
        lock.lock()
        ring.removeAll()
        lock.unlock()
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

    /// Channel `c` of `buffer` copied into a `mono` buffer (tap buffers are only valid inside the
    /// callback). Interleaved input steps over the other channels.
    private static func channel(_ c: Int, of buffer: AVAudioPCMBuffer, mono: AVAudioFormat) -> AVAudioPCMBuffer {
        let frames = Int(buffer.frameLength)
        guard c < Int(buffer.format.channelCount),
              let out = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: buffer.frameLength)
        else { return buffer }
        out.frameLength = buffer.frameLength
        let interleaved = buffer.format.isInterleaved
        let stride = buffer.stride  // 1 when deinterleaved, the channel count when interleaved
        if let src = buffer.floatChannelData, let dst = out.floatChannelData {
            let from = interleaved ? src[0] + c : src[c]
            for f in 0..<frames { dst[0][f] = from[f * stride] }
        } else if let src = buffer.int16ChannelData, let dst = out.int16ChannelData {
            let from = interleaved ? src[0] + c : src[c]
            for f in 0..<frames { dst[0][f] = from[f * stride] }
        }
        return out
    }

    /// Peak, sum of squares and the channel-0 zero check of each node channel (float formats; at most
    /// 8), for the mic_signal and levels rows and the channel pick.
    private static func signal(_ buffer: AVAudioPCMBuffer) -> EchoCancel.MicSignal {
        var out = EchoCancel.MicSignal()
        let frames = Int(buffer.frameLength)
        guard frames > 0, let data = buffer.floatChannelData else { return out }
        let stride = buffer.stride
        let interleaved = buffer.format.isInterleaved
        let stats = (0..<min(8, Int(buffer.format.channelCount))).map { c in
            EchoCancel.channelStats(interleaved ? data[0] + c : data[c], frames: frames, stride: stride)
        }
        out.add(peak: stats.map(\.peak), sumSq: stats.map(\.sumSq), frames: frames, ch0Zero: stats.first?.zero ?? false)
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
