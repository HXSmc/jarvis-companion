import Foundation

/// Why a barge-in interrupted: the wake word again, mic level over the echo estimate, or the
/// same with AEC on.
public enum BargeReason: String { case wake, level, aec }

/// Per-100-ms-block echo gate: the mic may only count as a barge-in candidate when it sits over
/// what the audio out's echo would explain. The echo reference is the loudest out block of the
/// last `tailBlocks` (0.5 s): the speaker plays a buffer after the mixer tap saw it and the room
/// keeps ringing after it, so the mic's echo trails the out blocks (Mac live run 1: a sentence
/// gap with out 0 and the mic still ringing fired `barge_in reason=level` 3 times). The out->mic
/// coupling is learned as a slow EMA of mic/reference while the mic is under the threshold; blocks
/// over it never learn (they would teach the owner's voice as echo). `holdBlocks` blocks (0.3 s)
/// make the candidate; partials trail the audio, so a candidate stays usable for `wordLag`.
public struct EchoGate {
    public static let outFloor = 0.01, micFloor = 0.01, defaultMargin = 2.0, aecMargin = 1.0, holdBlocks = 3, learnRate = 0.1, startCoupling = 1.0, tailBlocks = 5
    /// How long after its last over block a candidate still confirms a partial (= TailGate.lag).
    public static let wordLag: TimeInterval = 1.2

    public private(set) var coupling: Double = EchoGate.startCoupling
    public let margin: Double
    public private(set) var candidateSince: Date?
    public private(set) var overBlocks = 0
    /// The newest block that was over the echo, and the newest block that completed a candidate run.
    public private(set) var lastOverAt: Date?
    public private(set) var lastCandidateAt: Date?
    /// Counters for the one-shot `barge_gate` debug note (numbers only).
    public private(set) var blocks = 0, overTotal = 0, longestRun = 0
    public private(set) var peakRatio = 0.0
    private var recentOut: [Double] = []

    public init(margin: Double = EchoGate.defaultMargin) {
        self.margin = margin
    }

    /// The echo the gate expects on the mic for a next block with this `out`.
    public func echoEstimate(nextOut out: Double) -> Double {
        coupling * max(recentOut.suffix(Self.tailBlocks - 1).max() ?? 0, out)
    }

    /// One finished 100 ms block, `at` = its end.
    public mutating func block(mic: Double, out: Double, at: Date) {
        let reference = max(recentOut.suffix(Self.tailBlocks - 1).max() ?? 0, out)
        recentOut = Array((recentOut + [out]).suffix(Self.tailBlocks))
        blocks += 1
        if reference >= EchoGate.outFloor { peakRatio = max(peakRatio, mic / (coupling * reference)) }
        let over = mic > max(margin * coupling * reference, EchoGate.micFloor)
        if over {
            if overBlocks == 0 { candidateSince = at }
            overBlocks += 1
            overTotal += 1
            longestRun = max(longestRun, overBlocks)
            lastOverAt = at
            if overBlocks >= Self.holdBlocks { lastCandidateAt = at }
        } else {
            overBlocks = 0
            candidateSince = nil
            if reference >= EchoGate.outFloor {
                coupling += EchoGate.learnRate * (mic / reference - coupling)  // slow EMA of the echo ratio
            }
        }
    }

    /// `holdBlocks` blocks of 100 ms = 0.3 s of mic the echo cannot explain.
    public var isCandidate: Bool { overBlocks >= Self.holdBlocks }

    /// A candidate now, or one that ended at most `wordLag` ago: the partial holding the owner's
    /// words arrives after he spoke them, often after the run already ended.
    public func candidate(at now: Date) -> Bool {
        isCandidate || lastCandidateAt.map { now.timeIntervalSince($0) <= Self.wordLag } ?? false
    }

    /// Any over block in the last `wordLag`: words heard since then may be the owner's.
    public func recentlyOver(at now: Date) -> Bool {
        lastOverAt.map { now.timeIntervalSince($0) <= Self.wordLag } ?? false
    }

    /// The gate's numbers for the debug page (fixed words and numbers only).
    public var summary: String {
        "coupling=\((coupling * 1000).rounded() / 1000) blocks=\(blocks) over=\(overTotal) "
            + "longest_run=\(longestRun) peak_ratio=\((peakRatio * 100).rounded() / 100)"
    }
}

/// Stop phrases: a spoken line that alone says "stop". A Python test parses this literal from the
/// source and compares it to the service list, so it must stay ONE flat literal on one line.
public enum StopPhrase {
    public static let phrases: [String] = ["stop", "stop it", "stop talking", "stop listening", "stop now", "ok stop", "okay stop", "that's all", "that's all for now", "that's it", "thanks that's all", "thank you that's all", "never mind", "nevermind", "be quiet", "quiet", "enough", "that's enough", "cancel", "cancel that", "cancel it", "forget it", "abort"]

    /// Wake-stripped, lower-cased, curly apostrophes straightened, punctuation to spaces,
    /// whitespace collapsed; trailing politeness dropped while something else is left.
    public static func normalized(_ text: String) -> String {
        var t = TurnEndpointer.stripWake(text).lowercased()
        t = t.replacingOccurrences(of: "\u{2019}", with: "'")
        for mark in [",", ";", ":", ".", "!", "?"] {
            t = t.replacingOccurrences(of: mark, with: " ")
        }
        var words = t.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let polite: [[String]] = [["please"], ["sir"], ["jarvis"], ["thanks"], ["thank", "you"]]
        var dropped = true
        while dropped, words.count > 1 {
            dropped = false
            for p in polite where words.count > p.count {
                if Array(words.suffix(p.count)) == p {
                    words.removeLast(p.count)
                    dropped = true
                    break
                }
            }
        }
        return words.joined(separator: " ")
    }

    public static func matches(_ text: String) -> Bool {
        phrases.contains(normalized(text))
    }
}

/// Word checks on the partial against the reply being spoken: the owner's words must not be
/// words the reply itself contains.
public enum BargeWords {
    /// Ordered words, lower case, apostrophes removed, split on non-letters/digits.
    /// Every number is one token "#": the reply text says "Change 1 of 3" or "a thousand years",
    /// the recogniser hears "one of three" or "1,000", and it mishears digits of its own echo
    /// ("500" for "509", "20" for "27"; Mac live runs 1 and 2: each confirmed a self-interrupt).
    /// A dotted acronym is one token ("B.C." = "bc").
    private static let numberWords: Set<String> = [
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
        "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "hundred", "thousand", "million", "billion",
    ]

    static func tokens(_ s: String) -> [String] {
        var plain = ""
        let chars = Array(s.lowercased())
        var acronymDot = -2
        for (i, c) in chars.enumerated() {
            if c == "'" || c == "\u{2019}" { continue }
            let between = i > 0 && i + 1 < chars.count
            // "1,000" and "3.5" are one number.
            if c == "," || c == ".", between, chars[i - 1].isNumber, chars[i + 1].isNumber { continue }
            // "B.C." is "bc": a dot after a lone letter (start of word or after such a dot).
            if c == ".", between, chars[i - 1].isLetter, chars[i + 1].isLetter,
               i < 2 || !chars[i - 2].isLetter || acronymDot == i - 2 {
                acronymDot = i
                continue
            }
            plain.append(c)
        }
        return plain.split(whereSeparator: { !($0.isLetter || $0.isNumber) }).map(String.init)
            .map { $0.allSatisfy(\.isNumber) || numberWords.contains($0) ? "#" : $0 }
    }

    /// Recogniser noise words: never counted as the owner's new words.
    private static let fillers: Set<String> = ["um", "umm", "uh", "uhh", "hmm", "mm", "mhm", "ah", "er", "erm", "oh"]

    /// A heard word the recogniser misspelled from the reply's echo ("dacletian" for "diocletian",
    /// "expelling" for "expelled", Mac live run 2) is the reply's word: a heard token of >= 5
    /// letters within 1 edit (2 from 8 letters) of a reply token, or with the same stem, becomes that
    /// token. Shorter words must match exactly, so the owner's "what"/"time" never vanish into
    /// "that"/"tim". When unsure the word is the reply's: the gate does not interrupt.
    static func snapped(_ heard: [String], to reply: [String]) -> [String] {
        let known = Set(reply)
        let candidates = known.filter { $0.count >= 4 && $0.allSatisfy(\.isLetter) }
        return heard.map { h in
            guard !known.contains(h), h.count >= 5, h.allSatisfy(\.isLetter) else { return h }
            let allowance = h.count >= 8 ? 2 : 1
            let hs = stem(h)
            var best: (String, Int)?
            for r in candidates where abs(r.count - h.count) <= max(allowance, 3) {
                let d = stem(r) == hs ? 0 : editDistance(h, r, limit: allowance)
                if d <= allowance, best.map({ d < $0.1 || (d == $0.1 && r < $0.0) }) ?? true { best = (r, d) }
            }
            return best?.0 ?? h
        }
    }

    private static func stem(_ w: String) -> String {
        for suffix in ["ing", "ed", "es", "ly", "s"] where w.hasSuffix(suffix) && w.count - suffix.count >= 4 {
            return String(w.dropLast(suffix.count))
        }
        return w
    }

    /// Levenshtein distance, or `limit + 1` as soon as it must exceed `limit`.
    private static func editDistance(_ a: String, _ b: String, limit: Int) -> Int {
        let x = Array(a), y = Array(b)
        if abs(x.count - y.count) > limit { return limit + 1 }
        var prev = Array(0...y.count)
        for i in 1...max(x.count, 1) where !x.isEmpty {
            var cur = [i] + Array(repeating: 0, count: y.count)
            for j in stride(from: 1, through: y.count, by: 1) {
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x[i - 1] == y[j - 1] ? 0 : 1))
            }
            if (cur.min() ?? 0) > limit { return limit + 1 }
            prev = cur
        }
        return prev[y.count]
    }

    /// Whitespace words of a partial: `confirms(freshFrom:)` counts in these.
    public static func wordCount(_ s: String) -> Int {
        s.split(whereSeparator: { $0.isWhitespace }).count
    }


    /// Only the partial's words from word `freshFrom` on count (the words heard since the gate last
    /// went over the echo; the whole barge transcript holds every misheard echo word of the reply
    /// so far), with their echo lead dropped (`ownerWords`). Then:
    /// (b) a stop phrase confirms only when none of its words is in the reply;
    /// (a) otherwise >= 2 words outside the reply and the words not a contiguous run of it.
    public static func confirms(partial: String, reply: [String], freshFrom: Int = 0) -> Bool {
        let raw = partial.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let fresh = raw.dropFirst(min(max(freshFrom, 0), raw.count)).joined(separator: " ")
        let owned = ownerWords(partial: fresh, reply: reply)
        let r = reply.flatMap(tokens)
        let p = snapped(tokens(owned), to: r)
        let rs = Set(r)
        if StopPhrase.matches(owned) {
            return !p.isEmpty && p.allSatisfy { !rs.contains($0) }
        }
        guard newWords(p, replySet: rs) >= 2 else { return false }
        return !isContiguous(p, in: r)
    }

    /// How many of the partial's fresh words (from `freshFrom`, echo lead dropped) the reply does
    /// not hold: the number the word check needs >= 2 of. For the `barge_gate` note (`max_new`).
    public static func newWordCount(partial: String, reply: [String], freshFrom: Int = 0) -> Int {
        let raw = partial.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let fresh = raw.dropFirst(min(max(freshFrom, 0), raw.count)).joined(separator: " ")
        let r = reply.flatMap(tokens)
        return newWords(snapped(tokens(ownerWords(partial: fresh, reply: reply)), to: r), replySet: Set(r))
    }

    private static func newWords(_ p: [String], replySet: Set<String>) -> Int {
        p.filter { !replySet.contains($0) && !fillers.contains($0) }.count
    }

    /// The owner's own words from a partial whose lead echoes the reply: drop the longest
    /// leading run of >= 2 tokens (compared by normalized token, empty ones skipped) that occurs
    /// contiguously in the reply.
    public static func ownerWords(partial: String, reply: [String]) -> String {
        let parts = partial.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let replyTokens = reply.flatMap(tokens)
        var words = parts
        while words.count >= 2,
              let drop = echoLead(words.map { snapped(tokens($0), to: replyTokens).joined() }, in: replyTokens) {
            words.removeFirst(drop)
        }
        return words.joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }

    /// The barge turn's text for the service: nil until a barge-in was confirmed (an unsure gate
    /// never starts a turn, A2), else the owner's words from word `freshFrom` on (the words before
    /// it were the reply's echo), nil when nothing of them is left.
    public static func turnText(final: String, reply: [String], confirmed: Bool, freshFrom: Int = 0) -> String? {
        guard confirmed else { return nil }
        let raw = final.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let fresh = raw.dropFirst(min(max(freshFrom, 0), raw.count)).joined(separator: " ")
        let owned = dropEchoAnswer(ownerWords(partial: fresh, reply: reply), reply: reply)
        return owned.isEmpty ? nil : owned
    }

    /// A heard text that reads as a Confirm answer (`TailGate.isAnswer`) and whose every word is in
    /// the line Jarvis was speaking is that line's echo, never the owner's yes/no/stop: "" (fail
    /// closed: a pending Confirm stays pending). "Shall I send … with subject Yes?" must not answer
    /// itself. Every path where speech heard during or right after a reply reaches the service runs
    /// it: wake barge (`wakeTurnText`), level barge and a stop said over the reply (`turnText`),
    /// follow-up replay (AppController.handleWake).
    public static func dropEchoAnswer(_ text: String, reply: [String]) -> String {
        let replyTokens = reply.flatMap(tokens)
        let heard = snapped(tokens(TurnEndpointer.dropMisheardWake(TurnEndpointer.stripWake(text))), to: replyTokens)
        guard !heard.isEmpty, TailGate.isAnswer(text) else { return text }
        let replyWords = Set(replyTokens)
        return heard.allSatisfy(replyWords.contains) ? "" : text
    }

    /// A wake turn that cut a reply (A4) replays ring audio holding the reply's echo before and
    /// after the wake word, which STT may mishear ("the overlay, sir. Hey, John. The overlay. So"):
    /// drop the echo lead, a misheard wake, then the echo lead again; an answer made only of the
    /// reply's words is echo too (`dropEchoAnswer`).
    public static func wakeTurnText(final: String, reply: [String]) -> String {
        let lead = ownerWords(partial: final, reply: reply)
        let unwoken = TurnEndpointer.dropMisheardWake(lead)
            .trimmingCharacters(in: CharacterSet(charactersIn: " ,.!?;:-"))
        return dropEchoAnswer(ownerWords(partial: unwoken, reply: reply), reply: reply)
    }

    /// The longest leading run of >= 2 non-empty normalized tokens contiguous in the reply,
    /// as the number of leading elements to drop.
    private static func echoLead(_ norms: [String], in reply: [String]) -> Int? {
        var best: Int?
        for end in norms.indices where !norms[end].isEmpty {
            let run = norms[...end].filter { !$0.isEmpty }
            guard run.count >= 2, isContiguous(run, in: reply) else { continue }
            best = end + 1
        }
        return best
    }

    private static func isContiguous(_ run: [String], in reply: [String]) -> Bool {
        guard run.count <= reply.count else { return false }
        return (0...(reply.count - run.count)).contains { start in
            zip(reply[start..<(start + run.count)], run).allSatisfy { $0 == $1 }
        }
    }
}

/// The barge-in call: when unsure, never interrupt (nil).
public enum BargeDecision {
    public static func decide(gateCandidate: Bool, partial: String?, reply: [String],
                              wake: Bool, enabled: Bool, aec: Bool, freshFrom: Int = 0) -> BargeReason? {
        if wake { return .wake }  // the wake word interrupts even when barge-in is off
        guard enabled else { return nil }
        if gateCandidate, let partial, !partial.isEmpty,
           BargeWords.confirms(partial: partial, reply: reply, freshFrom: freshFrom) {
            return aec ? .aec : .level
        }
        return nil
    }
}
