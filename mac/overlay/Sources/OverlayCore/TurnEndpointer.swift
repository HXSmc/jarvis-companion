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
