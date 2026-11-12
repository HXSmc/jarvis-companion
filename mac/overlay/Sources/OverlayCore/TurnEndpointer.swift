import Foundation

/// Decides when a spoken turn is over. SpeechAnalyzer partials lag the audio, and people pause
/// mid-sentence ("show me the last, uh ... debugging session"), so a turn ends only after a
/// silence with neither new text nor voiced audio, and that silence is longer when the words
/// so far trail off; a complete-looking sentence ends on text silence alone, room "voice"
/// (fan, TV) cannot hold it open.
public enum TurnEndpointer {
    /// Complete-looking sentence: end fast.
    public static let fast: TimeInterval = 0.7
    /// Trails off (filler, article, conjunction, comma): give the user ~2 s more.
    public static let extended: TimeInterval = 2.7
    /// No new text for this long ends the turn even if the mic still "hears voice" (fan, TV).
    public static let stuckText: TimeInterval = 4
    /// Text silence this long ends a complete-looking sentence even while the mic hears voice.
    public static let completeHold: TimeInterval = 1.5
    /// A follow-up replay starts this long after the reply drained: the reply's room echo dies.
    public static let echoTail: TimeInterval = 0.15

    /// A filler ends a turn as incomplete even when STT punctuates it ("the last, huh?").
    static let fillers: Set<String> = ["uh", "um", "er", "erm", "uhm", "huh", "hm", "hmm", "mm", "ah"]  // Mac STT: "uh" -> "huh"
    static let trailing: Set<String> = fillers.union([
        "like",
        "the", "a", "an", "my", "your", "our", "their", "his", "her", "its", "this", "these",
        "and", "or", "but", "so", "because", "if", "than", "then",
        "to", "of", "for", "with", "from", "about", "into", "at", "by", "in", "on",
        "last", "next", "plus", "minus", "times", "is", "are", "was", "what", "which",
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

    /// The words after the wake word (moved from SpeechInput unchanged, so tests can pin it).
    public static func stripWake(_ raw: String) -> String {
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

    public static func shouldEnd(text: String, lastTextAt: Date, lastVoiceAt: Date?, now: Date) -> Bool {
        let needed = silence(after: text)
        if now.timeIntervalSince(lastTextAt) >= max(stuckText, needed) { return true }
        // A complete-looking sentence ends on text silence alone; empty text never does.
        if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
           !looksIncomplete(text),
           now.timeIntervalSince(lastTextAt) >= completeHold { return true }
        let quietSince = max(lastTextAt, lastVoiceAt ?? .distantPast)
        return now.timeIntervalSince(quietSince) >= needed
    }

    /// Where follow-up replay begins: anything stamped earlier is the reply's own echo.
    public static func replayStart(drainedAt: Date) -> Date {
        drainedAt.addingTimeInterval(echoTail)
    }

    /// Which ring chunks a follow-up may replay; all false when the drain time is unknown.
    public static func replayable(stamps: [Date], drainedAt: Date?) -> [Bool] {
        guard let drainedAt else { return stamps.map { _ in false } }
        let start = replayStart(drainedAt: drainedAt)
        return stamps.map { $0 >= start }
    }
}

/// A spoken line the service pushed after its turn ended ("no box, the change is on line 3") while
/// Jarvis talked or the follow-up mic was open. Played then, it talks over the reply or the follow-up
/// mic hears it as the owner, and dropping it left silence (Mac run 2, B4). So it waits until the
/// window closes with no new turn; a new turn, or `maxAge` since it came, drops it.
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

    public init() {}

    /// The floor follows quiet levels down at once and creeps up slowly (~10 s at 21 ms buffers),
    /// so speech never lifts it; voiced = clearly above it.
    public mutating func isVoiced(rms: Double) -> Bool {
        let current = floor ?? rms
        let next = rms < current ? rms : current + (rms - current) * 0.002
        floor = next
        return rms > max(next * 3, next + 0.006)
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
    /// Room words are quieter than half the request's loudest voice (-6 dB); the service can change it
    /// (`OVERLAY_TAIL_LEVEL_RATIO`, sent on /overlay/health).
    public static let defaultRatio = 0.5
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
    /// Never cuts an answer: a bare yes, or cut-off words that take it back ("Yes. No, wait."),
    /// go to the service whole so its whole-utterance Confirm check still sees them.
    public func cut(text: String, now: Date) -> String? {
        let full = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let kept, let ownerAt, kept != full,
              now.timeIntervalSince(ownerAt) >= TurnEndpointer.completeHold
        else { return nil }
        let dropped = full.hasPrefix(kept) ? String(full.dropFirst(kept.count)) : full
        if Self.words(kept).isSubset(of: Self.yesWords) || !Self.words(dropped).isDisjoint(with: Self.retractWords) {
            return nil
        }
        return kept
    }

    static let yesWords: Set<String> = ["hey", "jarvis", "yes", "yeah", "yep", "ok", "okay", "sure", "please",
                                        "sir", "do", "it", "go", "ahead", "confirm", "confirmed"]
    static let retractWords: Set<String> = ["no", "nope", "not", "dont", "stop", "cancel", "wait",
                                            "hold", "abort", "never", "nevermind"]

    /// Lowercased words, apostrophes removed ("Don't" -> "dont").
    static func words(_ s: String) -> Set<String> {
        let plain = s.lowercased().filter { $0 != "'" && $0 != "\u{2019}" }
        return Set(plain.split { !$0.isLetter }.map(String.init))
    }
}
