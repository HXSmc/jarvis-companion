import Foundation
import XCTest
@testable import OverlayCore

/// BargeCheck only (Foundation): these also run on Linux in a scratch package. Word flags are
/// hand-made by `flags` below (the real ones come from BargeWords, tested in BargeInTests).
final class BargeCheckTests: XCTestCase {
    typealias W = BargeCheck.Words

    private func verdict(aec: Bool, over: Bool = true, cand: Bool, _ w: W) -> BargeCheck.Verdict {
        BargeCheck.decide(aec: aec, recentlyOver: over, candidate: cand, words: w)
    }

    /// Hub H7: the level part is 3 over blocks in a row (a candidate) on every run.
    func testRuleTable() {
        let name = W(freshName: true, newWords: 3)
        let stop = W(stop: true, stopClean: true, newWords: 1)
        XCTAssertEqual(verdict(aec: true, cand: true, name), .init(rule: .name, reject: .none))
        XCTAssertEqual(verdict(aec: true, cand: true, stop), .init(rule: .stop, reject: .none))
        // Over blocks, but 2 or fewer in a row: no hold.
        XCTAssertEqual(verdict(aec: true, cand: false, name).reject, .noHold)
        XCTAssertEqual(verdict(aec: true, cand: false, stop).reject, .noHold)
        XCTAssertEqual(verdict(aec: true, over: false, cand: false, name).reject, .notOver)
        // A stop word the reply holds, no fresh name.
        XCTAssertEqual(verdict(aec: true, cand: true, W(stop: true, stopClean: false, newWords: 0)).reject, .echoWords)
        // "jarvis" only as the reply's own word.
        XCTAssertEqual(verdict(aec: true, cand: true, W(nameInReply: true, newWords: 2)).reject, .echoWords)
        // "what time is it": new words, no name, no stop.
        XCTAssertEqual(verdict(aec: true, cand: true, W(newWords: 4)).reject, .noName)
        XCTAssertEqual(verdict(aec: false, cand: true, W(newWords: 2)), .init(rule: .words, reject: .none))
        XCTAssertEqual(verdict(aec: false, cand: false, W(newWords: 2)).reject, .noHold)
        // Headphones as at jfollow8: a stop phrase holding a reply word never cuts, new words or not.
        XCTAssertEqual(verdict(aec: false, cand: true, W(stop: true, stopClean: false, newWords: 3)),
                       .init(rule: nil, reject: .echoWords))
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

    private func blockTimes(_ count: Int) -> [Double] { (0..<count).map { Double($0 + 1) * 0.1 } }

    /// Replays 100 ms blocks (mic, out) and partials in time order the way SpeechInput does
    /// (a partial is checked when it arrives; the watch loop feeds blocks and re-checks the last
    /// partial after new over blocks, C2). Returns the time of the confirm and its rule, or nil.
    private func replay(mic: [Double], out: Double, coupling: Double, partials: [Partial],
                        reply: Set<String>, aec: Bool = true) -> (at: Double, rule: BargeCheck.Rule, tracker: BargeCheck.Tracker)? {
        let r = replayRun(mic: mic, out: out, coupling: coupling, partials: partials, reply: reply, aec: aec)
        return r.hit.map { ($0.at, $0.rule, r.tracker) }
    }

    /// `replay` that also hands back the tracker when nothing confirmed (the run-end counters).
    private func replayRun(mic: [Double], out: Double, coupling: Double, partials: [Partial], reply: Set<String>,
                           aec: Bool = true) -> (hit: (at: Double, rule: BargeCheck.Rule)?, tracker: BargeCheck.Tracker) {
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
                    return ((partials[next].at, rule), tracker)
                }
                next += 1
            }
            tracker.block(over: isOver(mic: m, out: out, coupling: coupling), at: t0.addingTimeInterval(blockEnd))
            if tracker.wantsRecheck(at: t0.addingTimeInterval(blockEnd)), !lastText.isEmpty,
               let rule = run(lastText, at: blockEnd, recheck: true) {
                return ((blockEnd, rule), tracker)
            }
        }
        return (nil, tracker)
    }

    /// The 6affea7 check (SpeechInput.checkBarge before jfollow9), for the replay's "before": a
    /// partial while the gate is quiet moves the fresh start past ALL its words; a check happens
    /// only on a new partial while the gate is a candidate; no re-check. Returns (checked, confirmed).
    private func baseReplay(mic: [Double], out: Double, coupling: Double, partials: [Partial],
                            reply: Set<String>) -> (checked: Int, confirmed: Bool) {
        // Headphones tracker: its recentlyOver/candidate = 6affea7's EchoGate (1.2 s hold memory).
        var gate = BargeCheck.Tracker(aec: false)
        let t0 = Date(timeIntervalSince1970: 5000)
        var freshFrom = 0, checked = 0, next = 0
        for (i, m) in mic.enumerated() {
            let blockEnd = Double(i + 1) * 0.1
            while next < partials.count, partials[next].at <= blockEnd {
                let now = t0.addingTimeInterval(partials[next].at)
                let words = partials[next].text.split(separator: " ").map(String.init)
                next += 1
                guard gate.recentlyOver(at: now) else {
                    freshFrom = words.count
                    continue
                }
                guard gate.candidate(at: now) else { continue }
                checked += 1
                let w = flags(words, from: freshFrom, reply: reply)
                if (w.stop && w.stopClean) || (w.freshName && w.newWords >= 2) { return (checked, true) }
            }
            gate.block(over: isOver(mic: m, out: out, coupling: coupling), at: t0.addingTimeInterval(blockEnd))
        }
        return (checked, false)
    }

    /// The owner's first tries of 2026-11-15, replayed with the Mac-local row numbers (hub H7).
    /// Real: 17:01:36 `blocks=197 over=15 longest_run=5 partials=10 checked=0 coupling=0.003
    /// mic_p90=0.004 out_p90=0.200`; 17:08:13 `blocks=193 over=17 longest_run=7 partials=6
    /// checked=1 rule=stop`; the owner's processed-stream level 0.045 (his follow-ups 0.045-0.079).
    /// Hand-typed (the rows carry no per-block or per-partial times, no transcript): where the
    /// over blocks sit and when each partial lands.
    func testOwnerFirstTry() {
        let reply: Set<String> = ["the", "empire", "grew", "under", "trajan", "and", "its", "roads"]
        let owner = 0.045
        // 17:01:36. "Hey, Jarvis, stop." at 6.0-6.9 s: the canceller damps the double talk to 2
        // over blocks per word; partials 0.8 s after each word. He goes on talking at 8.1 s and makes
        // the run of 5 (8.1-8.5 s), but the recogniser's next partials only land 1.3 s and more after
        // it (volatile text revised at the pause), and 4 lone blips follow. 15 over blocks, 10 partials.
        var mic = Array(repeating: 0.004, count: 197)
        for i in [60, 61, 64, 65, 68, 69, 80, 81, 82, 83, 84, 120, 130, 140, 150] { mic[i] = owner }
        let phrase = [Partial(at: 7.0, text: "Hey"), Partial(at: 7.4, text: "Hey Jarvis"),
                      Partial(at: 7.8, text: "Hey Jarvis stop")]
        let later = (0..<7).map { Partial(at: 9.9 + Double($0) * 0.3, text: "Hey Jarvis stop" + String(repeating: " and", count: $0 + 1)) }
        XCTAssertEqual(mic.filter { isOver(mic: $0, out: 0.2, coupling: 0.003) }.count, 15)
        let base = baseReplay(mic: mic, out: 0.2, coupling: 0.003, partials: phrase + later, reply: reply)
        XCTAssertEqual(base.checked, 0)  // the row: checked=0, the reply played to its end
        XCTAssertFalse(base.confirmed)
        let fixed = replay(mic: mic, out: 0.2, coupling: 0.003, partials: phrase + later, reply: reply)
        XCTAssertEqual(fixed?.rule, .stop)
        XCTAssertEqual(fixed?.at ?? 99, 8.3, accuracy: 0.001)  // C2: the re-check when the run reaches 3
        XCTAssertEqual(fixed?.tracker.cmdRejects, 0)
        // 17:08:13. "Hey, Jarvis, stop." said twice. The first phrase's 2-block bursts end at 6.0 s;
        // its last partials (the text revised at the pause) land 1.4-2.0 s after, the gate quiet
        // (base moved the fresh start past them, C1). The repeat holds the run of 7 at 8.1-8.7 s
        // and its one partial lands at 9.5 s. 17 over blocks, 6 partials.
        var mic2 = Array(repeating: 0.006, count: 193)
        for i in [50, 51, 54, 55, 58, 59, 80, 81, 82, 83, 84, 85, 86, 100, 120, 140, 160] { mic2[i] = owner }
        let twice = [Partial(at: 6.6, text: "Hey"), Partial(at: 7.0, text: "Hey Jarvis"),
                     Partial(at: 7.4, text: "Hey Jarvis stop"), Partial(at: 7.8, text: "Hey, Jarvis, stop"),
                     Partial(at: 8.0, text: "Hey, Jarvis, stop."),
                     Partial(at: 9.5, text: "Hey, Jarvis, stop. Hey, Jarvis, stop.")]
        XCTAssertEqual(mic2.filter { isOver(mic: $0, out: 0.215, coupling: 0.005) }.count, 17)
        let base2 = baseReplay(mic: mic2, out: 0.215, coupling: 0.005, partials: twice, reply: reply)
        XCTAssertEqual(base2.checked, 1)  // the row: checked=1, confirmed on the repeat
        XCTAssertTrue(base2.confirmed)
        let fixed2 = replay(mic: mic2, out: 0.215, coupling: 0.005, partials: twice, reply: reply)
        XCTAssertEqual(fixed2?.rule, .stop)
        XCTAssertEqual(fixed2?.at ?? 99, 8.3, accuracy: 0.001)  // on the first phrase's words, before the repeat's partial
        XCTAssertEqual(fixed2?.tracker.freshFrom, 1)  // kept at "Jarvis" (C1), not moved past the phrase
        XCTAssertEqual(fixed2?.tracker.cmdRejects, 0)
    }

    /// Without a run of 3 the damped phrase alone does not confirm (hub H7 keeps the hold): only
    /// over blocks in a row count, on every run.
    func testTwoBlocksDoNotHold() {
        var mic = Array(repeating: 0.004, count: 60)
        for i in [10, 11, 14, 15, 18, 19] { mic[i] = 0.045 }
        XCTAssertNil(replay(mic: mic, out: 0.2, coupling: 0.003, partials: [
            Partial(at: 1.9, text: "Hey Jarvis"), Partial(at: 2.3, text: "Hey Jarvis stop"),
        ], reply: ["the", "empire"]))
    }

    /// Jarvis's own words never confirm: echo partials of the reply only, with residual echo
    /// blips over the gate in 2 non-adjacent blocks. And a stale command word kept from a quiet
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

    /// Round 5: owner row 20:59:22, replayed with its numbers. Decisive check `over=5 cand=0 run=0
    /// win=1 last_over_ms=203 mic=0.001 out=0.154 coup=0.003 ratio=0.76 fresh=0 words=3 new=3 name=0
    /// stop=1`; run end `blocks=290 over=14 longest_run=7 partials=6 checked=0 skip_over=0
    /// skip_hold=6`. The numbers force: 6 judge calls for 6 partials = no C2 re-check, so no over
    /// block after the first partial; `skip_over=0` = all 6 partials within 1.2 s of the block 203 ms
    /// before the first; `cand=0` = the last run of 3 ended more than 1.2 s before it. They do not
    /// say where: a run of 3 ending 1.4 s before (fixed by the 1.5 s memory, timeline 1) or the run
    /// of 7's tail 1.7 s before (still no_hold, timeline 2; `hold_age_ms` in the next rows decides).
    /// Hand-typed: the runs' places, the blips, the 3-word stop text.
    func testOwnerRow205922() {
        let reply: Set<String> = ["you'd", "need", "to", "open", "a", "claude", "code", "session"]
        let coupling = 0.003, out = 0.154, first = 27.103  // the newest over block ends at 26.9 s
        func mic(_ over: [Int]) -> [Double] {
            var m = Array(repeating: 0.001, count: 290)
            for i in over { m[i] = 0.02 }
            return m
        }
        // 1: the run of 7 at 20.1-20.7 s, a run of 3 at 25.5-25.7 s, blips; 2: the run of 7 at 24.8-25.4 s.
        let short = mic([49, 99, 200, 201, 202, 203, 204, 205, 206, 254, 255, 256, 264, 268])
        let tail7 = mic([49, 99, 149, 179, 209, 247, 248, 249, 250, 251, 252, 253, 264, 268])
        let partials = (0..<6).map { Partial(at: first + Double($0) * 0.15, text: "Okay okay stop") }
        for m in [short, tail7] {
            XCTAssertEqual(m.filter { isOver(mic: $0, out: out, coupling: coupling) }.count, 14)
            // e887b64's hold memory (`wordLag`, the headphones tracker keeps it): all 6 partials no_hold.
            let old = replayRun(mic: m, out: out, coupling: coupling, partials: partials, reply: reply, aec: false)
            XCTAssertNil(old.hit)
            XCTAssertEqual(old.tracker.checked, 0)
            XCTAssertEqual(old.tracker.skipNoHold, 6)
            XCTAssertEqual(old.tracker.skipNotOver, 0)
        }
        let fixed = replayRun(mic: short, out: out, coupling: coupling, partials: partials, reply: reply)
        XCTAssertEqual(fixed.hit?.rule, .stop)
        XCTAssertEqual(fixed.hit?.at ?? 99, first, accuracy: 0.001)  // the first phrase's first partial
        let tail = replayRun(mic: tail7, out: out, coupling: coupling, partials: partials, reply: reply)
        XCTAssertNil(tail.hit)  // 1.7 s: past the 1.5 s memory
        XCTAssertEqual(tail.tracker.skipNoHold, 6)
        for (m, age, res) in [(short, 1403.0, "res=confirm why=none rule=stop over=5 cand=1"),
                              (tail7, 1703.0, "res=reject why=no_hold rule=none over=5 cand=0")] {
            var tracker = BargeCheck.Tracker(aec: true)
            let t0 = Date(timeIntervalSince1970: 5000)
            for (i, v) in m.enumerated() where Double(i + 1) * 0.1 <= first {
                tracker.block(over: isOver(mic: v, out: out, coupling: coupling), at: t0.addingTimeInterval(Double(i + 1) * 0.1))
            }
            let now = t0.addingTimeInterval(first)
            XCTAssertEqual(tracker.overInWindow(at: now), 5)
            XCTAssertEqual(tracker.run, 0)
            XCTAssertEqual(Double(tracker.holdAgeMs(at: now) ?? 0), age, accuracy: 1.5)
            let words = ["Okay", "okay", "stop"]
            let row = tracker.check(at: now, wordCount: 3, recheck: false,
                                    commandIndex: { self.commandIndex(words, from: $0, reply: reply) },
                                    words: { self.flags(words, from: $0, reply: reply) }).row ?? ""
            XCTAssertTrue(row.hasPrefix(res + " run=0 win=1 last_over_ms=20"), row)
            XCTAssertTrue(row.contains(" fresh=0 words=3 new=3 name=0 stop=1 hold_age_ms="), row)
        }
    }

    /// Round 5, own words stay safe with the hold memory: (a) the reply's own "stop"/"Jarvis" with
    /// blips and no run of 3 never stops it, nor with a run in the memory (echo words); (b) a run
    /// older than `aecHoldMemory` and a late fresh stop word is no_hold; inside the memory it confirms.
    func testHoldMemoryKeepsOwnWordsSafe() {
        let reply: Set<String> = ["jarvis", "will", "stop", "the", "build"]
        var blips = Array(repeating: 0.001, count: 60)
        for i in [20, 22, 24, 26] { blips[i] = 0.02 }
        XCTAssertNil(replay(mic: blips, out: 0.2, coupling: 0.005, partials: [
            Partial(at: 2.5, text: "Jarvis will stop"), Partial(at: 2.75, text: "Jarvis will stop the build"),
        ], reply: reply))
        var run = blips
        for i in 10...12 { run[i] = 0.02 }  // a run of 3 ending at 1.3 s, inside the memory at 2.5 s
        let echo = replayRun(mic: run, out: 0.2, coupling: 0.005, partials: [
            Partial(at: 2.5, text: "Jarvis will stop"), Partial(at: 2.75, text: "Jarvis will stop the build"),
        ], reply: reply)
        XCTAssertNil(echo.hit)
        XCTAssertGreaterThan(echo.tracker.checked, 0)  // read, and turned down by the word rule
        // (b) the run ends at 1.3 s, a blip at 2.5 s keeps the gate over.
        var late = Array(repeating: 0.001, count: 60)
        for i in [10, 11, 12, 24] { late[i] = 0.02 }
        let words = ["okay", "stop"]
        func verdict(at: Double) -> BargeCheck.Verdict {
            var tracker = BargeCheck.Tracker(aec: true)
            let t0 = Date(timeIntervalSince1970: 5000)
            for (i, m) in late.enumerated() where Double(i + 1) * 0.1 <= at + 1e-9 {
                tracker.block(over: isOver(mic: m, out: 0.2, coupling: 0.005), at: t0.addingTimeInterval(Double(i + 1) * 0.1))
            }
            return tracker.check(at: t0.addingTimeInterval(at), wordCount: 2, recheck: false,
                                 commandIndex: { self.commandIndex(words, from: $0, reply: reply) },
                                 words: { self.flags(words, from: $0, reply: ["the", "build"]) }).verdict
        }
        XCTAssertEqual(verdict(at: 2.85).reject, .noHold)  // 1.55 s after the run: past the memory
        XCTAssertEqual(verdict(at: 2.75).rule, .stop)  // 1.45 s: inside
    }

    /// The longest row stays within the debug note cap and holds only fixed words and numbers.
    func testRowFits() {
        let w = W(freshName: true, nameInReply: true, stop: true, stopClean: true, newWords: 99_999)
        let row = BargeCheck.row(.init(rule: .words, reject: .echoWords), overInWindow: 99_999, candidate: true,
                                 run: 99_999, recentlyOver: true, lastOverMs: 9_999_999,
                                 level: .init(mic: 123, out: 123, coupling: 123, ratio: 1e9),
                                 freshFrom: 99_999, wordCount: 99_999, words: w, holdAgeMs: 9_999_999)
        XCTAssertLessThanOrEqual(row.count, 200, row)
        XCTAssertNotNil(row.range(of: #"^res=(confirm|reject) why=\w+ rule=\w+( [a-z_]+=[0-9.]+)+$"#,
                                  options: .regularExpression), row)
        let reject = BargeCheck.row(.init(rule: nil, reject: .noHold), overInWindow: 1, candidate: false, run: 1,
                                    recentlyOver: true, lastOverMs: 300,
                                    level: .init(mic: 0.031, out: 0.12, coupling: 0.05, ratio: 4.1),
                                    freshFrom: 0, wordCount: 3, words: W(freshName: true, stop: true, newWords: 3))
        XCTAssertEqual(reject, "res=reject why=no_hold rule=none over=1 cand=0 run=1 win=1 last_over_ms=300 "
                       + "mic=0.031 out=0.120 coup=0.050 ratio=4.10 fresh=0 words=3 new=3 name=1 stop=1 "
                       + "hold_age_ms=99999")
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
        XCTAssertEqual(tracker.skipNotOver, 7)  // the gate was quiet: waits, not word-rule rejects
        XCTAssertEqual(tracker.cmdRejects, 2)  // the kept word expired twice (after 2 s, at 3 s and 6 s): lost
        for k in 1...3 { tracker.block(over: true, at: t.addingTimeInterval(7 + Double(k) * 0.1)) }
        let confirm = tracker.check(at: t.addingTimeInterval(7.4), wordCount: 2, recheck: false,
                                    commandIndex: { _ in 0 }, words: { _ in W(freshName: true, newWords: 2) },
                                    level: .init())
        XCTAssertEqual(confirm.verdict.rule, .name)
        XCTAssertTrue(confirm.row?.hasPrefix("res=confirm why=none rule=name ") ?? false)
    }
}
