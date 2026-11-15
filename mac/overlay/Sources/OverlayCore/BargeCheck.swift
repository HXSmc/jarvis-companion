import Foundation

/// The barge-in call on one partial, as pure code (Foundation only, so its tests also run on
/// Linux). SpeechInput feeds it every gate block (over the echo or not) and every partial's word
/// flags (from `BargeWords`); it answers with the rule that confirms, or why it rejected, and the
/// `barge_check` debug row. Owner rows 2026-11-15: a single "Hey Jarvis, stop" needed a repeat
/// because the first phrase's late partial counted as echo (17:08:13: `rule=stop` with the phrase
/// twice in the turn) and a held candidate was never read by a partial (17:01:36: `longest_run=5`,
/// `partials=10`, `checked=0`).
public enum BargeCheck {
    /// = EchoGate.holdBlocks and EchoGate.wordLag (BargeIn.swift): copied, not imported, so this
    /// file builds alone on Linux; `BargeInTests.testBargeCheckMatchesEchoGate` pins them equal.
    /// The level part is the gate's 3 over blocks in a row on every run (hub H7: the owner's rows
    /// had runs of 5-12, so the hold was not what turned him down).
    public static let holdBlocks = 3
    public static let wordLag: TimeInterval = 1.2
    /// C1: a wake name or stop word heard while the gate was quiet stays fresh this long (its over
    /// blocks reach the gate after the partial); then it counts as echo. C2: the last partial is
    /// read again when over blocks come within this long of it.
    /// shortcut: hand-set from the owner's 2026-11-15 rows; recalibrate from live `barge_check` rows.
    public static let keepFor: TimeInterval = 2.0
    /// `barge_check` rows per run; later ones only count (`capped=` on the run's `barge_gate` row).
    public static let maxRows = 4

    public enum Rule: String { case name, stop, words }
    public enum Reject: String {
        case notOver = "not_over", noHold = "no_hold", echoWords = "echo_words", noName = "no_name",
             fewWords = "few_words", none
    }

    /// Word flags of a partial's fresh words (from `freshFrom` on, echo lead dropped).
    public struct Words: Equatable {
        /// "jarvis" heard and not in the reply / heard but the reply holds it too.
        public var freshName = false, nameInReply = false
        /// The words are a stop phrase, or end in one after fillers, "no"/"okay", wake words or
        /// the phrase again; `stopClean`: none of their words is in the reply.
        public var stop = false, stopClean = false
        /// Words the reply does not hold (fillers not counted); the words are one run of the reply.
        public var newWords = 0
        public var contiguous = false
        public init(freshName: Bool = false, nameInReply: Bool = false, stop: Bool = false,
                    stopClean: Bool = false, newWords: Int = 0, contiguous: Bool = false) {
            self.freshName = freshName
            self.nameInReply = nameInReply
            self.stop = stop
            self.stopClean = stopClean
            self.newWords = newWords
            self.contiguous = contiguous
        }
        /// The owner may be giving the command: a name or stop word is in the fresh words.
        public var decisive: Bool { freshName || nameInReply || stop }
    }

    /// The word rule. Through the canceller (`speakers`) only a clean stop phrase or a fresh
    /// "jarvis" cuts; a stop phrase holding a reply word falls through to the name rule.
    /// Headphones: a clean stop phrase, or >= 2 new words that are not one run of the reply.
    public static func wordRule(_ w: Words, speakers: Bool) -> Rule? {
        if w.stop && w.stopClean { return .stop }
        if speakers {
            return w.freshName && ((w.newWords >= 2 && !w.contiguous) || w.stop) ? .name : nil
        }
        return w.newWords >= 2 && !w.contiguous ? .words : nil
    }

    /// Why the word rule said no (only called when it did).
    static func wordReject(_ w: Words, speakers: Bool) -> Reject {
        if speakers {
            if w.freshName { return .fewWords }
            if w.nameInReply || w.stop { return .echoWords }
            return .noName
        }
        if w.contiguous || (w.stop && !w.stopClean) { return .echoWords }
        return .fewWords
    }

    /// The level numbers of the gate's newest block, for the row only.
    public struct Level: Equatable {
        public var mic = 0.0, out = 0.0, coupling = 0.0, ratio = 0.0
        public init(mic: Double = 0, out: Double = 0, coupling: Double = 0, ratio: Double = 0) {
            self.mic = mic
            self.out = out
            self.coupling = coupling
            self.ratio = ratio
        }
    }

    public struct Verdict: Equatable {
        public var rule: Rule?
        public var reject: Reject
    }

    /// The call: level part (an over block within `wordLag`, then a 3-in-a-row candidate within
    /// `wordLag`), then the word rule.
    public static func decide(aec: Bool, recentlyOver: Bool, candidate: Bool, words: Words) -> Verdict {
        guard recentlyOver else { return Verdict(rule: nil, reject: .notOver) }
        guard candidate else { return Verdict(rule: nil, reject: .noHold) }
        guard let rule = wordRule(words, speakers: aec) else {
            return Verdict(rule: nil, reject: wordReject(words, speakers: aec))
        }
        return Verdict(rule: rule, reject: .none)
    }

    /// One `barge_check` row: fixed words and numbers only (no transcript), <= 200 chars.
    public static func row(_ v: Verdict, overInWindow: Int, candidate: Bool, run: Int, recentlyOver: Bool,
                           lastOverMs: Int?, level: Level, freshFrom: Int, wordCount: Int,
                           words: Words) -> String {
        func n(_ x: Double, _ digits: Int, cap: Double) -> String { String(format: "%.\(digits)f", min(max(x, 0), cap)) }
        func i(_ x: Int) -> String { String(min(max(x, 0), 999)) }
        return "res=\(v.rule != nil ? "confirm" : "reject") why=\(v.reject.rawValue) rule=\(v.rule?.rawValue ?? "none") "
            + "over=\(i(overInWindow)) cand=\(candidate ? 1 : 0) run=\(i(run)) win=\(recentlyOver ? 1 : 0) "
            + "last_over_ms=\(min(max(lastOverMs ?? 99999, 0), 99999)) mic=\(n(level.mic, 3, cap: 9.999)) "
            + "out=\(n(level.out, 3, cap: 9.999)) coup=\(n(level.coupling, 3, cap: 9.999)) "
            + "ratio=\(n(level.ratio, 2, cap: 9999.99)) fresh=\(i(freshFrom)) words=\(i(wordCount)) "
            + "new=\(i(words.newWords)) name=\(words.freshName ? 1 : 0) stop=\(words.stop ? 1 : 0)"
    }

    /// One barge run's decision state beside the EchoGate: the over blocks (the gate's, fed
    /// block by block), the C1 kept word, the counters and the row budget. SpeechInput owns one
    /// per `BargeRun` and reads the level part from the gate itself (`recentlyOver`/`candidate`
    /// here give the same answers from the same blocks, for the Linux replay); `check` is the
    /// same order as `SpeechInput.checkBarge`.
    public struct Tracker {
        public let aec: Bool
        public private(set) var run = 0
        public private(set) var lastOverAt: Date?, lastCandidateAt: Date?
        /// End times of the over blocks of the last `keepFor` (the row's `over=`).
        public private(set) var overAt: [Date] = []
        public private(set) var overCount = 0
        /// First check whose fresh words held the command, and the commands turned down before the
        /// confirm: word-rule rejects plus kept words that expired (the `barge_in` first-try numbers).
        public private(set) var commandAt: Date?
        public private(set) var cmdRejects = 0
        /// Checks that read the words (candidate), and checks skipped before the word rule.
        public private(set) var checked = 0, skipNotOver = 0, skipNoHold = 0
        public private(set) var rows = 0, capped = 0
        public private(set) var lastPartialAt: Date?
        /// `check` keeps its own fresh-word start (SpeechInput keeps `BargeRun.freshFrom`).
        public private(set) var freshFrom = 0
        private var checkedOverCount = 0
        private var kept: (index: Int, end: Int, at: Date)?

        public init(aec: Bool) { self.aec = aec }

        /// One finished 100 ms gate block, `at` = its end.
        public mutating func block(over: Bool, at: Date) {
            guard over else {
                run = 0
                return
            }
            run += 1
            overCount += 1
            lastOverAt = at
            if run >= BargeCheck.holdBlocks { lastCandidateAt = at }
            overAt.append(at)
            overAt.removeAll { at.timeIntervalSince($0) > BargeCheck.keepFor }
        }

        public func recentlyOver(at now: Date) -> Bool {
            lastOverAt.map { now.timeIntervalSince($0) <= BargeCheck.wordLag } ?? false
        }

        public func candidate(at now: Date) -> Bool {
            run >= BargeCheck.holdBlocks || lastCandidateAt.map { now.timeIntervalSince($0) <= BargeCheck.wordLag } ?? false
        }

        public func overInWindow(at now: Date) -> Int {
            overAt.filter { now.timeIntervalSince($0) <= BargeCheck.keepFor }.count
        }

        /// C2: the gate gained an over block since the last check and the last partial is recent:
        /// its words are read again (they arrived before their over blocks).
        public func wantsRecheck(at now: Date) -> Bool {
            overCount > checkedOverCount
                && lastPartialAt.map { now.timeIntervalSince($0) <= BargeCheck.keepFor } ?? false
        }

        /// Before each check: a kept word older than `keepFor` is echo now (the new fresh start).
        public mutating func expire(at now: Date, freshFrom: Int, wordCount: Int) -> Int {
            guard let k = kept, now.timeIntervalSince(k.at) > BargeCheck.keepFor else { return freshFrom }
            kept = nil
            cmdRejects += 1  // a command the gate never held: lost
            return max(freshFrom, min(k.end, wordCount))
        }

        /// C1, the gate quiet: on an aec run the first wake name or stop word at or after
        /// `freshFrom` that the reply lacks (`commandIndex`) stays fresh: its index, else nil (the
        /// caller moves the fresh start past every word, as at base).
        public mutating func keep(at now: Date, freshFrom: Int, wordCount: Int,
                                  commandIndex: (Int) -> Int?) -> Int? {
            guard aec, let index = commandIndex(freshFrom), index < wordCount else { return nil }
            if kept?.index != index { kept = (index, wordCount, now) } else { kept?.end = wordCount }
            return index
        }

        /// The decision on one check and its counters and row (`recheck`: the last partial again).
        public mutating func judge(at now: Date, recheck: Bool, recentlyOver: Bool, candidate: Bool,
                                   freshFrom: Int, wordCount: Int, words w: Words,
                                   level: Level) -> (verdict: Verdict, row: String?) {
            if !recheck { lastPartialAt = now }
            checkedOverCount = overCount
            let verdict = BargeCheck.decide(aec: aec, recentlyOver: recentlyOver, candidate: candidate, words: w)
            switch verdict.reject {
            case .notOver: skipNotOver += 1
            case .noHold: skipNoHold += 1
            default: checked += 1
            }
            guard w.decisive else { return (verdict, nil) }
            if commandAt == nil && (w.freshName || w.stop) { commandAt = now }
            // A word-rule no on a partial counts; not_over/no_hold wait for blocks (C1/C2 keep the
            // words), so they count only when the kept word expires (`expire`).
            if verdict.rule == nil, !recheck, verdict.reject != .notOver, verdict.reject != .noHold { cmdRejects += 1 }
            guard rows < BargeCheck.maxRows || verdict.rule != nil else {
                capped += 1
                return (verdict, nil)
            }
            rows += 1
            let lastMs = lastOverAt.map { Int(now.timeIntervalSince($0) * 1000) }
            return (verdict, BargeCheck.row(verdict, overInWindow: overInWindow(at: now), candidate: candidate,
                                            run: run, recentlyOver: recentlyOver, lastOverMs: lastMs,
                                            level: level, freshFrom: freshFrom, wordCount: wordCount, words: w))
        }

        /// One whole check from this tracker's own blocks (tests and the replay): the order of
        /// `SpeechInput.checkBarge`.
        public mutating func check(at now: Date, wordCount: Int, recheck: Bool, commandIndex: (Int) -> Int?,
                                   words: (Int) -> Words, level: Level = Level()) -> (verdict: Verdict, row: String?) {
            freshFrom = expire(at: now, freshFrom: freshFrom, wordCount: wordCount)
            let over = recentlyOver(at: now)
            if !over {
                freshFrom = keep(at: now, freshFrom: freshFrom, wordCount: wordCount,
                                 commandIndex: commandIndex) ?? wordCount
            }
            return judge(at: now, recheck: recheck, recentlyOver: over, candidate: over && candidate(at: now),
                         freshFrom: freshFrom, wordCount: wordCount, words: words(freshFrom), level: level)
        }
    }
}
