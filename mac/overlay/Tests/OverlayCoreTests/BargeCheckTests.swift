import Foundation
import XCTest
@testable import OverlayCore

/// BargeCheck only (Foundation): these also run on Linux in a scratch package. Word flags are
/// hand-made by `flags` below (the real ones come from BargeWords, tested in BargeInTests).
final class BargeCheckTests: XCTestCase {
    typealias W = BargeCheck.Words

    private func verdict(aec: Bool, over: Bool = true, cand: Bool = false, inWindow: Int, _ w: W) -> BargeCheck.Verdict {
        BargeCheck.decide(aec: aec, recentlyOver: over, candidate: cand, overInWindow: inWindow, words: w)
    }

    func testRuleTable() {
        let name = W(freshName: true, newWords: 3)
        let stop = W(stop: true, stopClean: true, newWords: 1)
        XCTAssertEqual(verdict(aec: true, inWindow: 2, name), .init(rule: .name, reject: .none))
        XCTAssertEqual(verdict(aec: true, inWindow: 2, stop), .init(rule: .stop, reject: .none))
        XCTAssertEqual(verdict(aec: true, inWindow: 1, name).reject, .noHold)
        XCTAssertEqual(verdict(aec: true, over: false, inWindow: 0, name).reject, .notOver)
        // A stop word the reply holds, no fresh name.
        XCTAssertEqual(verdict(aec: true, cand: true, inWindow: 3, W(stop: true, stopClean: false, newWords: 0)).reject, .echoWords)
        // "jarvis" only as the reply's own word.
        XCTAssertEqual(verdict(aec: true, cand: true, inWindow: 3, W(nameInReply: true, newWords: 2)).reject, .echoWords)
        // "what time is it": new words, no name, no stop.
        XCTAssertEqual(verdict(aec: true, cand: true, inWindow: 4, W(newWords: 4)).reject, .noName)
        XCTAssertEqual(verdict(aec: false, cand: true, inWindow: 3, W(newWords: 2)), .init(rule: .words, reject: .none))
        XCTAssertEqual(verdict(aec: false, cand: false, inWindow: 2, W(newWords: 2)).reject, .noHold)
    }

    // MARK: replay

    /// One echo-gate block as BargeIn.swift's EchoGate.block judges it (aecMargin 1.0, micFloor
    /// 0.01, coupling held fixed as a learned one would be).
    private func isOver(mic: Double, out: Double, coupling: Double) -> Bool {
        mic > max(1.0 * coupling * out, 0.01)
    }

    /// Hand word flags for whitespace words from `from` on: "jarvis"/"stop" as the rules see them.
    private func flags(_ words: [String], from: Int, reply: Set<String>) -> W {
        let fresh = words.dropFirst(min(from, words.count)).map {
            $0.lowercased().trimmingCharacters(in: .punctuationCharacters)
        }
        let named = fresh.contains("jarvis")
        let lead: Set<String> = ["hey", "no", "okay", "jarvis"]
        let stop = fresh.last == "stop" && fresh.allSatisfy { $0 == "stop" || lead.contains($0) }
        let newWords = fresh.filter { !reply.contains($0) }.count
        return W(freshName: named && !reply.contains("jarvis"), nameInReply: named && reply.contains("jarvis"),
                 stop: stop, stopClean: stop && fresh.allSatisfy { !reply.contains($0) }, newWords: newWords)
    }

    private func commandIndex(_ words: [String], from: Int, reply: Set<String>) -> Int? {
        guard from < words.count else { return nil }
        return (from..<words.count).first { i in
            let w = words[i].lowercased().trimmingCharacters(in: .punctuationCharacters)
            return (w == "jarvis" || w == "stop") && !reply.contains(w)
        }
    }

    private struct Partial { let at: Double; let text: String }

    /// Replays 100 ms blocks (mic, out) and partials in time order the way SpeechInput does
    /// (feed blocks, re-check after new over blocks, check each partial). Returns the time of the
    /// confirm and its rule, or nil.
    private func replay(mic: [Double], out: Double, coupling: Double, partials: [Partial],
                        reply: Set<String>, aec: Bool = true) -> (at: Double, rule: BargeCheck.Rule, tracker: BargeCheck.Tracker)? {
        var tracker = BargeCheck.Tracker(aec: aec)
        let t0 = Date(timeIntervalSince1970: 5000)
        var next = 0
        var lastText = ""
        func run(_ text: String, at: Double, recheck: Bool) -> BargeCheck.Rule? {
            let words = text.split(separator: " ").map(String.init)
            let v = tracker.check(at: t0.addingTimeInterval(at), wordCount: words.count, recheck: recheck,
                                  commandIndex: { self.commandIndex(words, from: $0, reply: reply) },
                                  words: { self.flags(words, from: $0, reply: reply) },
                                  level: .init(mic: 0.05, out: out, coupling: coupling, ratio: 10)).verdict
            return v.rule
        }
        for (i, m) in mic.enumerated() {
            let blockEnd = Double(i + 1) * 0.1
            while next < partials.count, partials[next].at <= blockEnd {
                lastText = partials[next].text
                if let rule = run(lastText, at: partials[next].at, recheck: false) {
                    return (partials[next].at, rule, tracker)
                }
                next += 1
            }
            tracker.block(over: isOver(mic: m, out: out, coupling: coupling), at: t0.addingTimeInterval(blockEnd))
            if tracker.wantsRecheck(at: t0.addingTimeInterval(blockEnd)), !lastText.isEmpty,
               let rule = run(lastText, at: blockEnd, recheck: true) {
                return (blockEnd, rule, tracker)
            }
        }
        return nil
    }

    /// The owner's three first tries of 2026-11-15 (16:58:16, 17:01:55, 17:08:13), each said once.
    /// Real numbers from the Mac-local rows where they exist: coupling 0.005 and out 0.215
    /// (17:08:13 barge_gate), the owner's processed-stream level 0.045 (his follow-up levels were
    /// 0.045-0.079). Hand-typed: the block timeline (only 2 non-adjacent owner blocks per word, as
    /// the canceller damps double talk) and the partial texts and times (lag 0.8 s, the rows
    /// carry no transcript). Each confirms on its FIRST phrase.
    func testOwnerFirstTry() {
        let reply: Set<String> = ["the", "empire", "grew", "under", "trajan", "and", "its", "roads"]
        let residual = 0.001  // 0.005 * 0.215: what the canceller leaves of the reply
        let owner = 0.045
        // Echo partial first, then the owner's phrase: words at 1.0-1.3 s ("Hey"), 1.4-1.7 s
        // ("Jarvis"), 1.8-2.1 s ("stop"); over blocks 2 per word, never 3 in a row.
        var mic = Array(repeating: residual, count: 60)
        for i in [10, 12, 14, 16, 18, 20] { mic[i] = owner }
        let echo = "the empire grew under"
        // 1) 16:58:16 "Hey, Jarvis, stop." with partials 0.8 s after each word.
        let first = replay(mic: mic, out: 0.215, coupling: 0.005, partials: [
            Partial(at: 0.9, text: echo),
            Partial(at: 2.1, text: echo + " Hey"),
            Partial(at: 2.5, text: echo + " Hey Jarvis"),
            Partial(at: 2.9, text: echo + " Hey Jarvis stop"),
        ], reply: reply)
        XCTAssertNotNil(first)
        XCTAssertLessThanOrEqual(first?.at ?? 99, 2.9 + 0.01)
        XCTAssertEqual(first?.tracker.cmdRejects, 0)
        // 2) 17:01:55 "No, no, Jarvis, stop." where the partial lands BEFORE the blocks of its last
        // word are fed (C2): confirmed by the re-check, not by a repeat.
        var mic2 = Array(repeating: residual, count: 60)
        for i in [21, 23] { mic2[i] = owner }  // "No, no," left no over block; "Jarvis, stop." 2
        let second = replay(mic: mic2, out: 0.215, coupling: 0.005, partials: [
            Partial(at: 0.9, text: echo),
            Partial(at: 1.9, text: echo + " No, no,"),
            Partial(at: 2.15, text: echo + " No, no, Jarvis, stop."),
        ], reply: reply)
        XCTAssertNotNil(second)
        XCTAssertEqual(second?.rule, .stop)
        XCTAssertEqual(second?.at ?? 99, 2.4, accuracy: 0.001)
        XCTAssertEqual(second?.tracker.cmdRejects, 2)  // not_over at the partial, no_hold at 1 block
        // 3) 17:08:13 "Hey, Jarvis, stop." whose partial trails its last over block by 1.4 s
        // (C1: the gate is quiet by then; base moved freshFrom past the words).
        let third = replay(mic: mic, out: 0.215, coupling: 0.005, partials: [
            Partial(at: 0.9, text: echo),
            Partial(at: 3.5, text: echo + " Hey Jarvis stop"),
        ], reply: reply)
        XCTAssertEqual(third?.rule, .stop)
        XCTAssertEqual(third?.at, 3.5)
        XCTAssertEqual(third?.tracker.freshFrom, 5)  // kept at "Jarvis", not moved past the phrase
    }

    /// The same first try with only the 3-in-a-row hold (headphones rule, as the aec run had at
    /// base): the damped double talk never holds 3 blocks, so nothing confirms.
    func testThreeInARowMissesTheDampedPhrase() {
        var mic = Array(repeating: 0.001, count: 60)
        for i in [10, 12, 14, 16, 18, 20] { mic[i] = 0.045 }
        XCTAssertNil(replay(mic: mic, out: 0.215, coupling: 0.005, partials: [
            Partial(at: 2.5, text: "Hey Jarvis"), Partial(at: 2.9, text: "Hey Jarvis stop"),
        ], reply: ["the", "empire"], aec: false))
    }

    /// Jarvis's own words never confirm: echo partials of the reply only, with residual echo
    /// blips over the gate in 2 blocks of the window. And a stale command word kept from a quiet
    /// gate does not confirm on a later blip.
    func testOwnWordsNeverConfirm() {
        let blips: [Double] = {
            var m = Array(repeating: 0.001, count: 60)
            for i in [20, 22, 40, 42] { m[i] = 0.02 }
            return m
        }()
        // "Hey Jervis, Travis says the build is ready": a misheard "Jarvis" is the reply's word
        // (BargeWords.echoLike, pinned in BargeInTests), so it reaches BargeCheck as nameInReply.
        let jervis: Set<String> = ["hey", "jervis", "jarvis", "travis", "says", "the", "build", "is", "ready"]
        XCTAssertNil(replay(mic: blips, out: 0.2, coupling: 0.005, partials: [
            Partial(at: 1.0, text: "Hey"), Partial(at: 2.2, text: "Hey Jarvis, Travis"),
            Partial(at: 2.4, text: "Hey Jarvis, Travis says the build is ready"),
        ], reply: jervis))
        let ready: Set<String> = ["jarvis", "is", "ready", "when", "you", "are"]
        for _ in 0..<3 {
            XCTAssertNil(replay(mic: blips, out: 0.2, coupling: 0.005, partials: [
                Partial(at: 2.1, text: "Jarvis is"), Partial(at: 2.3, text: "Jarvis is ready when you are"),
                Partial(at: 4.3, text: "Jarvis is ready when you are"),
            ], reply: ready))
        }
        // The reply says "stop": its echo "stop" is echo_words.
        let stopReply: Set<String> = ["please", "stop", "the", "build"]
        var tracker = BargeCheck.Tracker(aec: true)
        let t = Date(timeIntervalSince1970: 100)
        for k in 1...4 { tracker.block(over: true, at: t.addingTimeInterval(Double(k) * 0.1)) }
        let words = ["stop"]
        let v = tracker.check(at: t.addingTimeInterval(0.5), wordCount: 1, recheck: false,
                              commandIndex: { self.commandIndex(words, from: $0, reply: stopReply) },
                              words: { self.flags(words, from: $0, reply: stopReply) }, level: .init())
        XCTAssertEqual(v.verdict.reject, .echoWords)
        // A fresh "stop" kept while the gate was quiet, then a blip 3 s later: stale, no confirm.
        let stale = replay(mic: blips.enumerated().map { $0.offset < 30 ? 0.001 : $0.element }, out: 0.2,
                           coupling: 0.005, partials: [Partial(at: 0.5, text: "the build stop"),
                                                        Partial(at: 4.35, text: "the build stop the")],
                           reply: ["the", "build"])
        XCTAssertNil(stale)
    }

    /// The longest row stays within the debug note cap and holds only fixed words and numbers.
    func testRowFits() {
        let w = W(freshName: true, nameInReply: true, stop: true, stopClean: true, newWords: 99_999)
        let row = BargeCheck.row(.init(rule: .words, reject: .echoWords), overInWindow: 99_999, candidate: true,
                                 run: 99_999, recentlyOver: true, lastOverMs: 9_999_999,
                                 level: .init(mic: 123, out: 123, coupling: 123, ratio: 1e9),
                                 freshFrom: 99_999, wordCount: 99_999, words: w)
        XCTAssertLessThanOrEqual(row.count, 200, row)
        XCTAssertNotNil(row.range(of: #"^res=(confirm|reject) why=\w+ rule=\w+( [a-z_]+=[0-9.]+)+$"#,
                                  options: .regularExpression), row)
        let reject = BargeCheck.row(.init(rule: nil, reject: .noHold), overInWindow: 1, candidate: false, run: 1,
                                    recentlyOver: true, lastOverMs: 300,
                                    level: .init(mic: 0.031, out: 0.12, coupling: 0.05, ratio: 4.1),
                                    freshFrom: 0, wordCount: 3, words: W(freshName: true, stop: true, newWords: 3))
        XCTAssertEqual(reject, "res=reject why=no_hold rule=none over=1 cand=0 run=1 win=1 last_over_ms=300 "
                       + "mic=0.031 out=0.120 coup=0.050 ratio=4.10 fresh=0 words=3 new=3 name=1 stop=1")
    }

    /// At most `maxRows` reject rows per run; the confirm always gets its row; the rest are counted.
    func testRowCap() {
        var tracker = BargeCheck.Tracker(aec: true)
        let t = Date(timeIntervalSince1970: 100)
        var rows = 0
        for k in 0..<7 {
            let r = tracker.check(at: t.addingTimeInterval(Double(k)), wordCount: 1, recheck: false,
                                  commandIndex: { _ in 0 }, words: { _ in W(freshName: true, newWords: 1) },
                                  level: .init())
            if r.row != nil { rows += 1 }
        }
        XCTAssertEqual(rows, BargeCheck.maxRows)
        XCTAssertEqual(tracker.capped, 3)
        XCTAssertEqual(tracker.cmdRejects, 7)
        for k in 1...3 { tracker.block(over: true, at: t.addingTimeInterval(7 + Double(k) * 0.1)) }
        let confirm = tracker.check(at: t.addingTimeInterval(7.4), wordCount: 2, recheck: false,
                                    commandIndex: { _ in 0 }, words: { _ in W(freshName: true, newWords: 2) },
                                    level: .init())
        XCTAssertEqual(confirm.verdict.rule, .name)
        XCTAssertTrue(confirm.row?.hasPrefix("res=confirm why=none rule=name ") ?? false)
    }
}
