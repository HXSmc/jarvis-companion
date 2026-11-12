import Foundation
import XCTest
@testable import OverlayCore

final class BargeInTests: XCTestCase {
    /// The reply's own echo never lifts the mic over the learned coupling: 60 blocks, no candidate.
    func testEchoAloneNeverCandidates() {
        var gate = EchoGate()
        var at = Date()
        for i in 0..<60 {
            let out = 0.05 + 0.15 * abs(sin(Double(i) * 0.37))
            let mic = 0.8 * out * (1 + sin(Double(i) * 1.7) * 0.1)
            at = at.addingTimeInterval(0.1)
            gate.block(mic: mic, out: out, at: at)
            XCTAssertFalse(gate.isCandidate, "block \(i)")
        }
    }

    /// 20 echo blocks, then the owner over the echo: candidate only after 3 blocks (0.3 s);
    /// one echo block resets it.
    func testOwnerOverEchoCandidatesAfterHold() {
        var gate = EchoGate()
        var at = Date(timeIntervalSince1970: 1000)
        func outLevel(_ i: Int) -> Double { 0.05 + 0.15 * abs(sin(Double(i) * 0.37)) }
        func micEcho(_ i: Int) -> Double { 0.8 * outLevel(i) * (1 + sin(Double(i) * 1.7) * 0.1) }
        for i in 0..<20 {
            at = at.addingTimeInterval(0.1)
            gate.block(mic: micEcho(i), out: outLevel(i), at: at)
        }
        XCTAssertFalse(gate.isCandidate)
        for i in 20..<23 {
            let out = outLevel(i)
            at = at.addingTimeInterval(0.1)
            gate.block(mic: 3 * gate.echoEstimate(nextOut: out), out: out, at: at)
            XCTAssertEqual(gate.isCandidate, i == 22, "block \(i)")
        }
        XCTAssertTrue(gate.isCandidate)
        XCTAssertNotNil(gate.candidateSince)
        at = at.addingTimeInterval(0.1)
        gate.block(mic: micEcho(23), out: outLevel(23), at: at)
        XCTAssertFalse(gate.isCandidate)
        XCTAssertNil(gate.candidateSince)
    }

    /// Words already in the reply are the reply's echo, never the owner's barge-in.
    func testWordsMustNotBeTheReply() {
        let reply = ["The Roman Empire lasted for centuries in the west."]
        XCTAssertFalse(BargeWords.confirms(partial: "the roman empire lasted", reply: reply))
        XCTAssertFalse(BargeWords.confirms(partial: "empire for", reply: reply))
        XCTAssertTrue(BargeWords.confirms(partial: "what time is it", reply: reply))
        XCTAssertTrue(BargeWords.confirms(partial: "stop", reply: reply))
        XCTAssertFalse(BargeWords.confirms(partial: "the", reply: reply))
        XCTAssertFalse(BargeWords.confirms(partial: "stop", reply: ["Please stop the timer."]))
    }

    func testOwnerWordsDropsEchoLead() {
        let reply = ["The Roman Empire lasted for centuries in the west."]
        XCTAssertEqual(BargeWords.ownerWords(partial: "the roman empire what time is it", reply: reply),
                       "what time is it")
        XCTAssertEqual(BargeWords.ownerWords(partial: "what time is it", reply: reply),
                       "what time is it")
    }

    /// Wake always wins; otherwise only candidate + words over the reply interrupt.
    func testDecision() {
        XCTAssertEqual(BargeDecision.decide(gateCandidate: true, partial: "what time is it",
                                            reply: [], wake: true, enabled: false, aec: false), .wake)
        XCTAssertNil(BargeDecision.decide(gateCandidate: true, partial: "what time is it",
                                          reply: [], wake: false, enabled: false, aec: false))
        XCTAssertNil(BargeDecision.decide(gateCandidate: false, partial: "what time is it",
                                          reply: [], wake: false, enabled: true, aec: false))
        XCTAssertNil(BargeDecision.decide(gateCandidate: true, partial: nil,
                                          reply: [], wake: false, enabled: true, aec: false))
        XCTAssertEqual(BargeDecision.decide(gateCandidate: true, partial: "what time is it",
                                            reply: [], wake: false, enabled: true, aec: false), .level)
        XCTAssertEqual(BargeDecision.decide(gateCandidate: true, partial: "what time is it",
                                            reply: [], wake: false, enabled: true, aec: true), .aec)
    }

    /// Politeness and punctuation strip away; words after the phrase keep it from matching.
    func testStopPhraseMatches() {
        for text in ["Stop.", "Hey Jarvis, stop.", "Jarvis stop", "Stop listening, please",
                     "That's all, thanks", "never mind"] {
            XCTAssertTrue(StopPhrase.matches(text), text)
        }
        for text in ["stop the server", "stop the timer in the code", "what does stop mean"] {
            XCTAssertFalse(StopPhrase.matches(text), text)
        }
    }

    /// Security R1: the legacy recogniser's time-cap final while the gate is unsure (Jarvis's own
    /// echo, misheard) must never become a wake-less turn.
    func testUnconfirmedFinalIsNeverATurn() {
        let reply = ["The emperor Augustus ruled Rome for forty years."]
        let echo = "the emperor augustus a gust us ruled rome"
        XCTAssertNil(BargeWords.turnText(final: echo, reply: reply, confirmed: false))
        XCTAssertNil(BargeWords.turnText(final: "what time is it", reply: reply, confirmed: false))
        XCTAssertEqual(BargeWords.turnText(final: "the emperor augustus what time is it", reply: reply,
                                           confirmed: true), "what time is it")
        XCTAssertNil(BargeWords.turnText(final: "the emperor augustus", reply: reply, confirmed: true))
    }

    // MARK: Mac live run 1 (round 3)

    /// Speech with sentence gaps; the mic hears it 2 blocks late (speaker latency) with a reverb
    /// tail. Live: `barge_in reason=level` fired 3 times with nobody speaking (a gap: out 0, the
    /// mic still ringing). The gate must never call that a candidate.
    func testDelayedEchoWithReverbNeverCandidates() {
        var gate = EchoGate()
        var at = Date(timeIntervalSince1970: 2000)
        var outs: [Double] = []
        for i in 0..<300 {
            let inGap = i % 40 >= 32  // 3.2 s sentences, 0.8 s gaps
            outs.append(inGap ? 0 : 0.04 + 0.16 * abs(sin(Double(i) * 0.9)))
            func o(_ k: Int) -> Double { k >= 0 ? outs[k] : 0 }
            let mic = 0.9 * o(i - 2) + 0.35 * o(i - 3) + 0.15 * o(i - 4) + 0.004
            at = at.addingTimeInterval(0.1)
            gate.block(mic: mic, out: outs[i], at: at)
            XCTAssertFalse(gate.isCandidate, "block \(i)")
        }
        XCTAssertFalse(gate.candidate(at: at))
    }

    /// The partial holding the owner's words lands after he spoke them: a run that ended under
    /// `wordLag` ago still counts, an older one does not.
    func testCandidateHoldsForTheWordLag() {
        var gate = EchoGate()
        var at = Date(timeIntervalSince1970: 3000)
        for _ in 0..<10 { at = at.addingTimeInterval(0.1); gate.block(mic: 0.08, out: 0.1, at: at) }
        for _ in 0..<3 {
            at = at.addingTimeInterval(0.1)
            gate.block(mic: 3 * gate.echoEstimate(nextOut: 0.1), out: 0.1, at: at)
        }
        XCTAssertTrue(gate.isCandidate)
        at = at.addingTimeInterval(0.1)
        gate.block(mic: 0.08, out: 0.1, at: at)
        XCTAssertFalse(gate.isCandidate)
        XCTAssertTrue(gate.candidate(at: at.addingTimeInterval(1.0)))
        XCTAssertFalse(gate.candidate(at: at.addingTimeInterval(1.5)))
        XCTAssertTrue(gate.summary.hasPrefix("coupling="))
    }

    /// Live: the 'why' reply 'Change 1 of 3 came from "Add the retry counter"' was heard as
    /// "Writer. Change one of three came from add the retry counter" and confirmed itself.
    func testNumberWordsAreTheReplysDigits() {
        let reply = ["Change 1 of 3 came from \"Add the retry counter\", committed 12 November 2026."]
        XCTAssertFalse(BargeWords.confirms(partial: "Writer. Change one of three came from add the retry counter",
                                           reply: reply))
        XCTAssertTrue(BargeWords.confirms(partial: "what time is it", reply: reply))
    }

    /// Only the words since the gate went over count: 20 s of transcript hold misheard echo words,
    /// and a "stop" after them is still a whole stop phrase.
    func testOnlyFreshWordsCount() {
        let reply = ["Rome began as a small city state on the Tiber.",
                     "It grew into a republic and then an empire that ruled the Mediterranean."]
        let echo = "allowed Rome began as a small city state on the tie bar it grew into a republic"
        let n = BargeWords.wordCount(echo)
        XCTAssertTrue(BargeWords.confirms(partial: echo + " and then", reply: reply),
                      "the whole transcript confirms on misheard echo words (the live self-interrupt)")
        XCTAssertFalse(BargeWords.confirms(partial: echo + " and then", reply: reply, freshFrom: n))
        XCTAssertTrue(BargeWords.confirms(partial: echo + " stop", reply: reply, freshFrom: n))
        XCTAssertTrue(BargeWords.confirms(partial: echo + " and then stop", reply: reply, freshFrom: n))
        XCTAssertTrue(BargeWords.confirms(partial: echo + " what time is it", reply: reply, freshFrom: n))
        XCTAssertEqual(BargeDecision.decide(gateCandidate: true, partial: echo + " stop", reply: reply,
                                            wake: false, enabled: true, aec: false, freshFrom: n), .level)
        // Live: the confirmed barge turn sent 35 s of Jarvis's own words as the owner's.
        XCTAssertEqual(BargeWords.turnText(final: echo + " what time is it", reply: reply, confirmed: true,
                                           freshFrom: n), "what time is it")
    }

    /// Live: a wake that cut the hide reply sent "the overlay, sir. Hey, John. The overlay. So".
    func testWakeTurnTextDropsTheCutReplyAndMisheardWake() {
        let reply = ["Hiding the overlay, sir.", "The overlay is unchanged."]
        XCTAssertEqual(BargeWords.wakeTurnText(final: "the overlay, sir. Hey, John. The overlay. So", reply: reply),
                       "So")
        XCTAssertEqual(BargeWords.wakeTurnText(final: "what time is it", reply: reply), "what time is it")
        XCTAssertEqual(BargeWords.wakeTurnText(final: "the overlay is unchanged", reply: reply), "")
    }

    // MARK: Round 4 (security): the reply's own echo never answers a pending Confirm

    private let confirmQuestion = ["Shall I send an email to bob@example.com with subject Yes?"]

    /// Wake barge (A4): the reviewer's input. Before the fix the result was "Yes." (a yes).
    func testWakeTurnTextDropsAnEchoedYes() {
        XCTAssertEqual(BargeWords.wakeTurnText(final: "send an email to Bob with subject. Hey, John. Yes.",
                                               reply: confirmQuestion), "")
        // "jarvis" heard: stripWake leaves the lone echo.
        XCTAssertEqual(BargeWords.wakeTurnText(final: "Hey Jarvis. Yes.", reply: confirmQuestion), "")
        // The owner's own answer, not in the reply, still passes.
        XCTAssertEqual(BargeWords.wakeTurnText(final: "send an email to Bob with subject. Hey, John. No.",
                                               reply: confirmQuestion), "No")
        XCTAssertEqual(BargeWords.wakeTurnText(final: "with subject. Hey, John. Stop.",
                                               reply: confirmQuestion), "Stop")
    }

    /// Level barge (A3): a confirmed barge turn whose words are only the echoed answer is no turn.
    func testBargeTurnTextDropsAnEchoedYes() {
        XCTAssertNil(BargeWords.turnText(final: "Yes.", reply: confirmQuestion, confirmed: true))
        XCTAssertNil(BargeWords.turnText(final: "with subject yes", reply: confirmQuestion, confirmed: true))
        XCTAssertEqual(BargeWords.turnText(final: "No.", reply: confirmQuestion, confirmed: true), "No.")
        XCTAssertEqual(BargeWords.turnText(final: "stop", reply: confirmQuestion, confirmed: true), "stop")
    }

    /// Stop over the reply: a "stop" that is the reply's own word is echo; the owner's stop passes.
    func testStopEchoIsDroppedOnEveryPath() {
        let reply = ["Shall I stop the server?"]
        XCTAssertNil(BargeWords.turnText(final: "stop", reply: reply, confirmed: true))
        XCTAssertEqual(BargeWords.wakeTurnText(final: "Hey Jarvis, stop.", reply: reply), "")
        XCTAssertEqual(BargeWords.dropEchoAnswer("Stop.", reply: reply), "")
        XCTAssertEqual(BargeWords.dropEchoAnswer("no", reply: reply), "no")
        XCTAssertEqual(BargeWords.turnText(final: "no", reply: reply, confirmed: true), "no")
    }

    /// Follow-up replay: the window after the question hears its tail; an echoed yes is dropped,
    /// the owner's yes (not a word of the question), no and stop pass, and a non-answer passes whole.
    func testFollowUpDropsAnEchoedAnswerOnly() {
        XCTAssertEqual(BargeWords.dropEchoAnswer("Yes.", reply: confirmQuestion), "")
        XCTAssertEqual(BargeWords.dropEchoAnswer("Hey, John. Yes.", reply: confirmQuestion), "")
        let plain = ["Shall I push the branch?"]
        XCTAssertEqual(BargeWords.dropEchoAnswer("Yes.", reply: plain), "Yes.")
        XCTAssertEqual(BargeWords.dropEchoAnswer("No.", reply: confirmQuestion), "No.")
        XCTAssertEqual(BargeWords.dropEchoAnswer("stop", reply: confirmQuestion), "stop")
        XCTAssertEqual(BargeWords.dropEchoAnswer("send an email", reply: confirmQuestion), "send an email")
        XCTAssertEqual(BargeWords.dropEchoAnswer("Yes.", reply: []), "Yes.")
        XCTAssertEqual(BargeWords.dropEchoAnswer("", reply: confirmQuestion), "")
    }
    // MARK: Round 5 (Mac live run 2): misheard echo words confirmed 5 self-interrupts

    /// Each pair = the reply Jarvis was speaking and the barge text STT heard from its own echo
    /// (nobody spoke). Before round 5 every one confirmed (>= 2 "new" words: "1" "000", "500"
    /// "years" "b" "c", "dacletian" "restook", "air" "20").
    func testMisheardEchoNeverConfirms() {
        let cases: [([String], String)] = [
            (["A big subject, sir, so I'll give you the full arc on cards and a short spoken summary.",
              "In short: Rome grew from a city to a Mediterranean republic, became an empire under Augustus, peaked under Trajan, and fractured in the third and fifth centuries. The East lasted until 1453, a thousand years longer than the West."],
             "1,000"),
            (["Rome began as a small city on the Tiber, traditionally founded in 753 BC, and became a republic in 509 BC after expelling its kings.",
              "It conquered Italy, then defeated Carthage in the Punic Wars and came to dominate the Mediterranean."],
             "in 500 years. B.C. after expelling its kings. It conquered Italy"),
            (["The third century brought civil war, plague and inflation, until Diocletian restored order."],
             "Dacletian restook"),
            (["His heir Octavian prevailed, and in 27 BC became Augustus, the first emperor."],
             "air Octavian prevailed, and in 20"),
            (["Instead, his heir Octavian became Augustus in 27 BC, the first emperor."],
             "Air Octavian became Augustus in 20"),
            (["It became a republic in 509 BC after expelling its kings, and slowly conquered Italy."],
             "it became a republic in 500 BC after expelled its kings"),
        ]
        for (reply, heard) in cases {
            XCTAssertFalse(BargeWords.confirms(partial: heard, reply: reply), heard)
            XCTAssertNil(BargeDecision.decide(gateCandidate: true, partial: heard, reply: reply,
                                              wake: false, enabled: true, aec: false), heard)
            XCTAssertLessThan(BargeWords.newWordCount(partial: heard, reply: reply), 2, heard)
        }
        // The owner's words over the same replies still confirm.
        for (reply, _) in cases {
            XCTAssertTrue(BargeWords.confirms(partial: "what time is it", reply: reply))
            XCTAssertTrue(BargeWords.confirms(partial: "stop", reply: reply))
            XCTAssertTrue(BargeWords.confirms(partial: "Augustus in 20 what time is it", reply: reply))
        }
    }

    /// Numbers are one token; acronym letters join; near spellings snap only from 5 letters.
    func testEchoTokenRules() {
        XCTAssertEqual(BargeWords.tokens("1,000 years B.C. a thousand 3.5"), ["#", "years", "bc", "a", "#", "#"])
        XCTAssertEqual(BargeWords.snapped(["dacletian", "expelling", "air", "what", "time"],
                                          to: BargeWords.tokens("Diocletian expelled their kings at that time")),
                       ["diocletian", "expelled", "air", "what", "time"])
        XCTAssertEqual(BargeWords.snapped(["weather"], to: ["rome"]), ["weather"])
        XCTAssertEqual(BargeWords.newWordCount(partial: "um uh what", reply: []), 1)
    }
}
