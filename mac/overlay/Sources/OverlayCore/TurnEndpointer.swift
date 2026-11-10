import Foundation

/// Decides when a spoken turn is over. SpeechAnalyzer partials lag the audio, and people pause
/// mid-sentence ("show me the last, uh ... debugging session"), so a turn ends only after a
/// silence with neither new text nor voiced audio, and that silence is longer when the words
/// so far trail off.
public enum TurnEndpointer {
    /// Complete-looking sentence: end fast.
    public static let fast: TimeInterval = 0.7
    /// Trails off (filler, article, conjunction, comma): give the user ~2 s more.
    public static let extended: TimeInterval = 2.7
    /// No new text for this long ends the turn even if the mic still "hears voice" (fan, TV).
    public static let stuckText: TimeInterval = 4

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

    public static func shouldEnd(text: String, lastTextAt: Date, lastVoiceAt: Date?, now: Date) -> Bool {
        let needed = silence(after: text)
        if now.timeIntervalSince(lastTextAt) >= max(stuckText, needed) { return true }
        let quietSince = max(lastTextAt, lastVoiceAt ?? .distantPast)
        return now.timeIntervalSince(quietSince) >= needed
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
