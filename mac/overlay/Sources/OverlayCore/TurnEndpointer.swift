import Foundation

/// Decides when a spoken turn is over. SpeechAnalyzer partials lag the audio, and people pause
/// mid-sentence ("show me the last, uh ... debugging session"), so a turn ends only after a
/// silence with neither new text nor voiced audio, and that silence is longer when the words
/// so far trail off; a short complete-looking command ends on text silence once the voice has
/// paused (room "voice" with gaps cannot hold it open), constant voice ends on stuckText.
public enum TurnEndpointer {
    /// Complete-looking sentence: end fast.
    public static let fast: TimeInterval = 0.7
    /// Trails off (filler, article, conjunction, comma): give the user ~2 s more.
    public static let extended: TimeInterval = 2.7
    /// No new text for this long ends the turn even if the mic still "hears voice" (fan, TV).
    public static let stuckText: TimeInterval = 4
    /// Text silence this long ends a short complete-looking command over room voice that has paused
    /// for holdVoiceQuiet (it never fires while the owner is heard).
    public static let completeHold: TimeInterval = 1.5
    /// completeHold applies only to a short command (wake word off): Mac run r2 cut 20-word
    /// requests at "Can you tell me when you have a moment" while the owner was still talking.
    /// A longer request with voice still heard ends on 0.7 s of quiet or stuckText instead.
    public static let completeHoldMaxWords = 4
    /// The hold also needs this much voice quiet: Mac run r3 (F8) partials stalled at "Hey Jarvis,
    /// can you tell me" for 1.5 s while the owner went on talking (voice quiet 0.01-0.07 s), and the
    /// hold cut the request. Above a comma pause; a steady fan is unvoiced (VoiceActivity floor).
    public static let holdVoiceQuiet: TimeInterval = 0.5
    /// A follow-up replay starts this long after the reply drained: the reply's room echo dies.
    public static let echoTail: TimeInterval = 0.15
    /// An unpunctuated partial this long (in words) is mid-request, not a finished sentence.
    public static let longRequestWords = 10  // above the 8-word finished commands of the short-command tests

    /// A filler ends a turn as incomplete even when STT punctuates it ("the last, huh?").
    static let fillers: Set<String> = ["uh", "um", "er", "erm", "uhm", "huh", "hm", "hmm", "mm", "ah"]  // Mac STT: "uh" -> "huh"
    static let trailing: Set<String> = fillers.union([
        "like",
        "the", "a", "an", "my", "your", "our", "their", "his", "her", "its", "this", "these",
        "and", "or", "but", "so", "because", "if", "than", "then",
        "to", "of", "for", "with", "from", "about", "into", "at", "by", "in", "on",
        "last", "next", "plus", "minus", "times", "is", "are", "was", "what", "which",
        "called", "named",  // Mac run r2: "say hello in a file called | voice-test"
    ])

    public static func looksIncomplete(_ text: String) -> Bool {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let last = t.last else { return false }
        if last == "," { return true }
        let word = t.split(whereSeparator: { !$0.isLetter && $0 != "'" }).last.map { $0.lowercased() } ?? ""
        if fillers.contains(word) { return true }
        if ".?!".contains(last) { return false }
        return trailing.contains(word)
    }

    public static func silence(after text: String) -> TimeInterval {
        looksIncomplete(text) ? extended : fast
    }

    /// A long partial without terminal punctuation is still growing (final results punctuate),
    /// so a natural pause inside it is not the end: it gets the extended window.
    static func midRequest(_ text: String) -> Bool {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let last = t.last, !".?!".contains(last) else { return false }
        return t.split { !$0.isLetter && $0 != "'" }.count >= longRequestWords
    }

    /// The words after the wake word (moved from SpeechInput unchanged, so tests can pin it).
    /// A wake turn drops everything through the first wake "jarvis" near the start: a "hey/hi/ok"
    /// lead-in, a clause break, or the line's start (the 2 s ring buffer can hold words said before
    /// the wake word); a "jarvis" preceded by an ordinary word, like "titled Jarvis", is part of the
    /// request and stays. A follow-up drops only a leading wake phrase: its turn already opened on
    /// the wake word, so a later "Jarvis" is a name, not a second wake.
    public static func stripWake(_ raw: String, followUp: Bool = false) -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let pattern = followUp
            ? #"^(?:(?:hey|hi|ok|okay)[\s,]+)?jarvis\b"#
            : #"(?:^|(?:\b(?:hey|hi|ok|okay)[\s,]+)|(?:[,.!?;]\s*))jarvis\b"#
        if let range = text.range(of: pattern, options: [.regularExpression, .caseInsensitive]),
           followUp || text.distance(from: text.startIndex,
                                     to: text.index(range.upperBound, offsetBy: -6)) < 40 {
            text = String(text[range.upperBound...])
        }
        return text
            .trimmingCharacters(in: CharacterSet(charactersIn: " ,.!?;:-"))
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// intents._MISHEARD_WAKE_RE: STT's version of "Hey Jarvis." ("Hey, John Pierce.") at the
    /// start is dropped too.
    public static func dropMisheardWake(_ text: String) -> String {
        var t = text
        if let r = t.range(of: #"^(?:hey|hi|ok(?:ay)?)(?:[\s,]+\w+){1,3}[.!?]\s+"#,
                           options: [.regularExpression, .caseInsensitive]) {
            t.removeSubrange(r)
        }
        return t
    }

    public static func shouldEnd(text: String, lastTextAt: Date, lastVoiceAt: Date?, now: Date) -> Bool {
        endRule(text: text, lastTextAt: lastTextAt, lastVoiceAt: lastVoiceAt, now: now) != nil
    }

    /// Which rule ends the turn now ("stuck", "hold", "fast", "extended"), or nil to keep
    /// listening. The name goes into the debug page's STT note so a cut turn says why.
    public static func endRule(text: String, lastTextAt: Date, lastVoiceAt: Date?, now: Date) -> String? {
        let mid = midRequest(text)
        let needed = mid ? extended : silence(after: text)
        if now.timeIntervalSince(lastTextAt) >= max(stuckText, needed) { return "stuck" }
        // A short complete-looking command ends on text silence over gappy room voice; empty text
        // never does, and never while the voice is still heard (stalled partials, F8).
        let voiceQuiet = now.timeIntervalSince(lastVoiceAt ?? .distantPast)
        if !mid, holdsComplete(text), now.timeIntervalSince(lastTextAt) >= completeHold,
           voiceQuiet >= holdVoiceQuiet { return "hold" }
        let quietSince = max(lastTextAt, lastVoiceAt ?? .distantPast)
        guard now.timeIntervalSince(quietSince) >= needed else { return nil }
        return needed > fast ? "extended" : "fast"
    }

    static func holdsComplete(_ text: String) -> Bool {
        let words = stripWake(text).split { !$0.isLetter && !$0.isNumber && $0 != "'" }.count
        return words > 0 && words <= completeHoldMaxWords && !looksIncomplete(text)
    }

    /// Where follow-up replay begins: anything stamped earlier is the reply's own echo.
    public static func replayStart(drainedAt: Date) -> Date {
        drainedAt.addingTimeInterval(echoTail)
    }

    /// The follow-up window's replay start: after the reply drained AND after the audio the last turn
    /// already heard. A dropped room turn has no reply, so the drain alone replayed its words into the
    /// next follow-up (Mac run 4: "Thought so. Exactly. Next change").
    public static func replayFrom(drainedAt: Date?, heardUntil: Date?) -> Date? {
        switch (drainedAt, heardUntil) {
        case let (d?, h?): return max(d, h)
        case let (d, h): return d ?? h
        }
    }

    /// Which ring chunks a follow-up may replay; all false when the drain time is unknown.
    public static func replayable(stamps: [Date], drainedAt: Date?) -> [Bool] {
        guard let drainedAt else { return stamps.map { _ in false } }
        let start = replayStart(drainedAt: drainedAt)
        return stamps.map { $0 >= start }
    }
}

/// When a spoken line the service pushed after its turn ended may sound.
public enum PushTiming: Equatable {
    /// Idle: say it now.
    case now
    /// The follow-up mic is open and nobody has spoken: close the window, say it, reopen the window
    /// after it (Mac run 4, B5 fold: held to the window's end, the line came >= 5.3 s late).
    case closeWindow
    /// A reply plays, the owner talks or a turn runs: hold it (`HeldLine`); the reply's drain or the
    /// window's close says it, a new turn drops it.
    case hold

    public static func decide(orb: OrbState, audioBusy: Bool) -> PushTiming {
        if audioBusy { return .hold }
        switch orb {
        case .idle, .error: return .now
        case .followUp: return .closeWindow
        case .listening, .thinking, .speaking: return .hold
        }
    }
}

/// A spoken line the service pushed after its turn ended ("no box, the change is on line 3") while
/// Jarvis talked or the follow-up mic was open. Played then, it talks over the reply or the follow-up
/// mic hears it as the owner, and dropping it left silence (Mac run 2, B4). So it waits for the reply
/// to drain or the window to close (`PushTiming`); a new turn, or `maxAge` since it came, drops it.
public struct HeldLine<Event> {
    public static var maxAge: TimeInterval { 15 }
    /// One line is a say and an audio event; a few lines at most.
    public static var maxEvents: Int { 4 }
    private var events: [Event] = []
    private var since: Date?

    public init() {}

    public mutating func hold(_ event: Event, at now: Date) {
        if events.isEmpty { since = now }
        if events.count < Self.maxEvents { events.append(event) }
    }

    /// The held events when still fresh, oldest first; nothing is held afterwards.
    public mutating func release(at now: Date) -> [Event] {
        defer { drop() }
        guard let since, now.timeIntervalSince(since) <= Self.maxAge else { return [] }
        return events
    }

    public mutating func drop() {
        events = []
        since = nil
    }
}

/// Per-buffer voice activity from mic RMS against a tracked noise floor.
public struct VoiceActivity {
    public private(set) var floor: Double?
    /// The recent loudest buffer: follows loud levels up at once, decays per buffer.
    public private(set) var peak: Double = 0
    static let peakDecay = 0.99
    /// A buffer this share of the recent peak (about -10 dB) and above the floor's margin is the
    /// same talker's softer syllable (Mac run r2: 100 ms buffers with a reverberant floor near a
    /// third of the speech level left gaps over 0.7 s inside one sentence).
    static let speechShare = 0.3

    public init() {}

    /// The floor follows quiet levels down at once and creeps up slowly (~10 s at 21 ms buffers),
    /// so speech never lifts it; voiced = clearly above it, or near the recent peak.
    public mutating func isVoiced(rms: Double) -> Bool {
        let current = floor ?? rms
        let next = rms < current ? rms : current + (rms - current) * 0.002
        floor = next
        peak = max(rms, peak * Self.peakDecay)
        if rms > max(next * 3, next + 0.006) { return true }
        return rms > next + 0.006 && rms >= peak * Self.speechShare
    }
}

/// One mic buffer's level: when it came, its RMS (0...1) and how many frames it held.
public struct LevelSample {
    public let at: Date
    public let rms: Double
    public let frames: Int

    public init(at: Date, rms: Double, frames: Int) {
        self.at = at
        self.rms = rms
        self.frames = frames
    }
}

/// The mic level in 100 ms blocks (each the RMS over its buffers). The service's follow-up gate reads
/// them (`LEVEL_BLOCK_MS` in gateway/overlay.py) and the tail gate compares new words with them.
public enum LevelBlocks {
    public static let width: TimeInterval = 0.1
    /// At most this many blocks go to the service (40 s; the service refuses more).
    public static let maxSent = 400

    /// Blocks from `from` up to `to`; a block with no buffer is 0. Rounded to 4 places.
    public static func blocks(_ samples: [LevelSample], from: Date, to: Date) -> [Double] {
        // Date sums carry ~1e-7 s of float error: 0.3 s must stay 3 blocks, not 4.
        let count = Int((to.timeIntervalSince(from) / width - 1e-4).rounded(.up))
        guard count > 0 else { return [] }
        var power = [Double](repeating: 0, count: count)
        var frames = [Int](repeating: 0, count: count)
        for s in samples where s.at >= from && s.at < to && s.frames > 0 {
            let i = min(count - 1, Int(s.at.timeIntervalSince(from) / width))
            power[i] += s.rms * s.rms * Double(s.frames)
            frames[i] += s.frames
        }
        return zip(power, frames).map { p, n in
            n > 0 ? ((p / Double(n)).squareRoot() * 10_000).rounded() / 10_000 : 0
        }
    }
}

/// After a complete wake request, words from quieter room talk must not extend or replace it (Mac
/// runs 2 and 3: room speech joined a request, or took the turn). Words count as the owner's only
/// when his voice, at `ratio` of the turn's loudest 100 ms block, was heard up to `lag` before them.
public struct TailGate {
    /// Room words glued onto a request sat within ~6 dB of the owner's loudest voice (jfollow4 E):
    /// only 0.6 of the request's peak (-4.4 dB) still counts as him. The service can change it
    /// (`OVERLAY_TAIL_LEVEL_RATIO`, sent on /overlay/health).
    public static let defaultRatio = 0.6
    /// Partials trail the audio: a word shows up to this long after it was said.
    public static let lag: TimeInterval = 1.2
    /// Blocks below this RMS are silence, never the owner's voice.
    public static let silence = 0.002

    public let ratio: Double
    public private(set) var peak = 0.0
    public private(set) var ownerAt: Date?
    /// The newest complete-looking text heard while the owner spoke.
    public private(set) var kept: String?

    public init(ratio: Double = TailGate.defaultRatio) {
        self.ratio = ratio > 0 && ratio <= 1 ? ratio : TailGate.defaultRatio
    }

    /// One finished 100 ms block, `at` = its end.
    public mutating func level(_ rms: Double, at: Date) {
        peak = max(peak, rms)
        if rms >= max(Self.silence, ratio * peak) { ownerAt = at }
    }

    public mutating func partial(_ text: String, at: Date) {
        guard let ownerAt, at.timeIntervalSince(ownerAt) <= Self.lag else { return }  // the room's words
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        kept = !t.isEmpty && !TurnEndpointer.looksIncomplete(t) ? t : nil
    }

    /// The request to end the turn with: it is complete, the owner has been quiet for
    /// `TurnEndpointer.completeHold`, and the words since then came from the room. Else nil.
    /// Never cuts an answer: a kept part that alone reads as a yes, no or cancel ("Yes, thank you."),
    /// or cut-off words that take it back ("Yes. No, wait."), go to the service whole so its
    /// whole-utterance Confirm check still sees them.
    public func cut(text: String, now: Date) -> String? {
        let full = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let kept, let ownerAt, kept != full,
              now.timeIntervalSince(ownerAt) >= TurnEndpointer.completeHold
        else { return nil }
        let dropped = full.hasPrefix(kept) ? String(full.dropFirst(kept.count)) : full
        if Self.isAnswer(kept) || !Self.words(dropped).isDisjoint(with: Self.retractWords) {
            return nil
        }
        return kept
    }

    /// `s` alone could read as a Confirm answer to the service: after its wake strips, every word is
    /// in `answerWords`. tests/test_overlay_jfollow2.py fails if this lags mac/voice/intents.py.
    static func isAnswer(_ s: String) -> Bool {
        words(TurnEndpointer.dropMisheardWake(TurnEndpointer.stripWake(s))).isSubset(of: answerWords)
    }

    /// Every word of intents._CONFIRM_RE, _CANCEL_RE, _NO_RE and _POLITE (apostrophes removed), plus
    /// wake and plain-yes words.
    static let answerWords: Set<String> = [
        "hey", "jarvis", "yes", "yeah", "yep", "ok", "okay", "sure", "please", "sir", "do", "it", "go",
        "ahead", "confirm", "confirmed", "thanks", "thank", "you", "no", "nope", "not", "dont", "stop",
        "cancel", "that", "thats", "never", "mind", "nevermind", "forget", "enough", "abort", "leave",
    ]
    static let retractWords: Set<String> = ["no", "nope", "not", "dont", "stop", "cancel", "wait",
                                            "hold", "abort", "never", "nevermind"]

    /// Lowercased words, apostrophes removed ("Don't" -> "dont").
    static func words(_ s: String) -> Set<String> {
        let plain = s.lowercased().filter { $0 != "'" && $0 != "\u{2019}" }
        return Set(plain.split { !$0.isLetter }.map(String.init))
    }
}

/// What a wake-word hit does given the app's state: cut the reply that plays or streams, ignore it
/// while the owner's turn is open (the words keep flowing into that turn), or open a new turn.
public enum WakeGate {
    public enum Action: Equatable { case newTurn, cutReply, ignore }

    public static func decide(audioBusy: Bool, streaming: Bool, listening: Bool,
                              followUp: Bool, hasSpeech: Bool) -> Action {
        if audioBusy || streaming { return .cutReply }
        if listening && !followUp { return .ignore }
        if listening && followUp && hasSpeech { return .ignore }
        return .newTurn
    }
}
