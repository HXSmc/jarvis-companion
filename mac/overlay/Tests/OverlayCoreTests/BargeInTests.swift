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
            gate.block(mic: 3 * gate.coupling * out, out: out, at: at)
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
}
