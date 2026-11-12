import Foundation

/// Why a barge-in interrupted: the wake word again, mic level over the echo estimate, or the
/// same with AEC on.
public enum BargeReason: String { case wake, level, aec }

/// Per-100-ms-block echo gate: the mic may only count as a barge-in candidate when it sits over
/// what the audio out's echo would explain. The out->mic coupling is learned as a slow EMA of the
/// echo ratio while the mic is under the threshold; blocks over it never learn (they would teach
/// the owner's voice as echo). `holdBlocks` blocks (0.3 s) make the candidate.
public struct EchoGate {
    public static let outFloor = 0.01, micFloor = 0.01, defaultMargin = 2.0, aecMargin = 1.0, holdBlocks = 3, learnRate = 0.1, startCoupling = 1.0

    public private(set) var coupling: Double = EchoGate.startCoupling
    public let margin: Double
    public private(set) var candidateSince: Date?
    public private(set) var overBlocks = 0

    public init(margin: Double = EchoGate.defaultMargin) {
        self.margin = margin
    }

    /// One finished 100 ms block, `at` = its end.
    public mutating func block(mic: Double, out: Double, at: Date) {
        let over = mic > max(margin * coupling * out, EchoGate.micFloor)
        if over {
            if overBlocks == 0 { candidateSince = at }
            overBlocks += 1
        } else {
            overBlocks = 0
            candidateSince = nil
            if out >= EchoGate.outFloor {
                coupling += EchoGate.learnRate * (mic / out - coupling)  // slow EMA of the echo ratio
            }
        }
    }

    /// `holdBlocks` blocks of 100 ms = 0.3 s of mic the echo cannot explain.
    public var isCandidate: Bool { overBlocks >= Self.holdBlocks }
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
    private static func tokens(_ s: String) -> [String] {
        let plain = s.lowercased().filter { $0 != "'" && $0 != "\u{2019}" }
        return plain.split { !($0.isLetter || $0.isNumber) }.map(String.init)
    }

    private static func normToken(_ s: String) -> String {
        tokens(s).joined()
    }

    /// (b) A stop phrase confirms only when none of its words is in the reply;
    /// (a) otherwise >= 2 words outside the reply and the partial not a contiguous
    /// sub-sequence of it.
    public static func confirms(partial: String, reply: [String]) -> Bool {
        let p = tokens(partial)
        let r = reply.flatMap(tokens)
        let rs = Set(r)
        if StopPhrase.matches(partial) {
            return p.allSatisfy { !rs.contains($0) }
        }
        guard p.filter({ !rs.contains($0) }).count >= 2 else { return false }
        return !isContiguous(p, in: r)
    }

    /// The owner's own words from a partial whose lead echoes the reply: drop the longest
    /// leading run of >= 2 tokens (compared by normalized token, empty ones skipped) that occurs
    /// contiguously in the reply.
    public static func ownerWords(partial: String, reply: [String]) -> String {
        let parts = partial.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let replyTokens = reply.flatMap(tokens)
        var words = parts
        while words.count >= 2, let drop = echoLead(words.map(normToken), in: replyTokens) {
            words.removeFirst(drop)
        }
        return words.joined(separator: " ").trimmingCharacters(in: .whitespaces)
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
                              wake: Bool, enabled: Bool, aec: Bool) -> BargeReason? {
        if wake { return .wake }  // the wake word interrupts even when barge-in is off
        guard enabled else { return nil }
        if gateCandidate, let partial, !partial.isEmpty,
           BargeWords.confirms(partial: partial, reply: reply) {
            return aec ? .aec : .level
        }
        return nil
    }
}
