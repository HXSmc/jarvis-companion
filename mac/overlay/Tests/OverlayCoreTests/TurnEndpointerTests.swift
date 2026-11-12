import Foundation
import XCTest
@testable import OverlayCore

final class TurnEndpointerTests: XCTestCase {
    /// Owner, 02:18:24: "Can you show me the last, uh" was ended by endOfSpeech after 0.7 s.
    func testTrailingOffWaitsLonger() {
        for text in ["Can you show me the last, uh", "Can you show me the last, huh", "the last, huh?", "show me the", "Open the", "What is two plus",
                     "and the,", "um"] {
            XCTAssertEqual(TurnEndpointer.silence(after: text), TurnEndpointer.extended, text)
        }
        for text in ["Can you show me the last debugging session", "What time is it",
                     "Tell me a joke.", "Open Safari"] {
            XCTAssertEqual(TurnEndpointer.silence(after: text), TurnEndpointer.fast, text)
        }
    }

    func testOneSecondPauseAfterFillerDoesNotEndTheTurn() {
        let t0 = Date()
        let cut = "Can you show me the last, uh"
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: cut, lastTextAt: t0, lastVoiceAt: t0,
                                                now: t0.addingTimeInterval(1.0)))
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: cut, lastTextAt: t0, lastVoiceAt: t0,
                                               now: t0.addingTimeInterval(2.8)))
        let done = "Can you show me the last debugging session"
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: done, lastTextAt: t0, lastVoiceAt: t0,
                                               now: t0.addingTimeInterval(0.75)))
    }

    func testStillVoicedKeepsListeningWhilePartialsLag() {
        let t0 = Date()
        // Text stalled for 1 s but the mic heard voice 0.2 s ago: the user is still talking.
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "Tell me a short", lastTextAt: t0,
                                                lastVoiceAt: t0.addingTimeInterval(0.8),
                                                now: t0.addingTimeInterval(1.0)))
        // Constant "voice" (a fan) never holds a turn open past 4 s of unchanged text.
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: "What time is it", lastTextAt: t0,
                                               lastVoiceAt: t0.addingTimeInterval(4),
                                               now: t0.addingTimeInterval(4)))
    }

    func testHeldLineWaitsForTheWindowToClose() {
        // Mac run 2, B4: "no box, the change is on line 3" came during the follow-up window and the
        // push dropped it. Held, it plays when the window closes; a new turn or 15 s drops it.
        let t0 = Date(timeIntervalSince1970: 1000)
        var held = HeldLine<String>()
        held.hold("say", at: t0)
        held.hold("audio", at: t0.addingTimeInterval(0.1))
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(8)), ["say", "audio"])
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(8)), [])  // said once
        held.hold("say", at: t0)
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(HeldLine<String>.maxAge + 1)), [])  // too old
        held.hold("say", at: t0)
        held.drop()  // the owner started a new turn
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(1)), [])
        for i in 0..<10 { held.hold("e\(i)", at: t0) }
        XCTAssertEqual(held.release(at: t0).count, HeldLine<String>.maxEvents)
    }

    func testStripWakeOneBreath() {
        XCTAssertEqual(TurnEndpointer.stripWake("Hey Jarvis next change"), "next change")
    }

    func testCompleteTextEndsDespiteRoomVoice() {
        let t0 = Date()
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "Highlight the changes.", lastTextAt: t0,
                                                 lastVoiceAt: t0.addingTimeInterval(1.4),
                                                 now: t0.addingTimeInterval(1.4)))
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: "Highlight the changes.", lastTextAt: t0,
                                                lastVoiceAt: t0.addingTimeInterval(1.6),
                                                now: t0.addingTimeInterval(1.55)))
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "show me the last", lastTextAt: t0,
                                                 lastVoiceAt: t0.addingTimeInterval(1.6),
                                                 now: t0.addingTimeInterval(2.0)))
        // Empty text never ends via the complete-hold rule.
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "", lastTextAt: t0,
                                                 lastVoiceAt: t0.addingTimeInterval(1.9),
                                                 now: t0.addingTimeInterval(1.9)))
    }

    func testReplayStartsAfterTheReplyAndEcho() {
        let d = Date()
        XCTAssertEqual(TurnEndpointer.replayStart(drainedAt: d), d.addingTimeInterval(0.15))
        let stamps = [d.addingTimeInterval(-0.5), d, d.addingTimeInterval(0.1),
                      d.addingTimeInterval(0.15), d.addingTimeInterval(0.4)]
        XCTAssertEqual(TurnEndpointer.replayable(stamps: stamps, drainedAt: d),
                       [false, false, false, true, true])
        XCTAssertEqual(TurnEndpointer.replayable(stamps: [d], drainedAt: nil), [false])
    }

    func testVoiceActivityTracksTheNoiseFloor() {
        var vad = VoiceActivity()
        for _ in 0..<100 { XCTAssertFalse(vad.isVoiced(rms: 0.002)) }  // quiet room
        XCTAssertTrue(vad.isVoiced(rms: 0.05))                          // speech
        for _ in 0..<50 { _ = vad.isVoiced(rms: 0.05) }
        XCTAssertTrue(vad.isVoiced(rms: 0.05))  // 1 s of speech does not become the floor
        XCTAssertFalse(vad.isVoiced(rms: 0.003))
    }

    // Round 6: a wake request keeps its words when quieter room talk follows (Mac runs 2-3).

    /// Feeds 100 ms blocks of `rms` from `start` for `seconds`.
    private func feed(_ gate: inout TailGate, _ rms: Double, from start: Date, seconds: Double) {
        var t = start
        while t < start.addingTimeInterval(seconds - 0.001) {
            t = t.addingTimeInterval(LevelBlocks.width)
            gate.level(rms, at: t)
        }
    }

    func testRoomWordsAfterACompleteRequestAreCut() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.12, from: t0, seconds: 1.5)                  // the owner: "Highlight the changes."
        gate.partial("Highlight the", at: t0.addingTimeInterval(1.0))
        gate.partial("Highlight the changes.", at: t0.addingTimeInterval(1.8))
        feed(&gate, 0.03, from: t0.addingTimeInterval(1.5), seconds: 3)  // the room, a quarter as loud
        gate.partial("Highlight the changes. What are you people", at: t0.addingTimeInterval(3.2))
        let text = "Highlight the changes. What are you people"
        XCTAssertNil(gate.cut(text: text, now: t0.addingTimeInterval(2.9)))  // owner quiet < 1.5 s
        XCTAssertEqual(gate.cut(text: text, now: t0.addingTimeInterval(3.05)), "Highlight the changes.")
    }

    func testOwnerContinuingAtHisLevelIsKept() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.12, from: t0, seconds: 1.5)
        gate.partial("Highlight the changes.", at: t0.addingTimeInterval(1.6))
        feed(&gate, 0.1, from: t0.addingTimeInterval(1.5), seconds: 1.0)  // he goes on, same distance
        gate.partial("Highlight the changes. And the next one.", at: t0.addingTimeInterval(2.9))
        XCTAssertEqual(gate.kept, "Highlight the changes. And the next one.")
        XCTAssertNil(gate.cut(text: "Highlight the changes. And the next one.", now: t0.addingTimeInterval(4.5)))
        // Late words of his own (STT lag under 1.2 s) are his, not the room's.
        var lagged = TailGate()
        feed(&lagged, 0.12, from: t0, seconds: 2.0)
        lagged.partial("show me the weather", at: t0.addingTimeInterval(1.2))
        lagged.partial("show me the weather in Paris", at: t0.addingTimeInterval(3.0))
        XCTAssertEqual(lagged.kept, "show me the weather in Paris")
        XCTAssertNil(lagged.cut(text: "show me the weather in Paris", now: t0.addingTimeInterval(5)))
    }

    // Round 8 (security): a quiet "no, wait" after "yes" must reach the service whole.
    func testARetractedYesIsNeverCut() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.10, from: t0, seconds: 0.4)                        // "Yes."
        gate.partial("Yes.", at: t0.addingTimeInterval(0.6))
        feed(&gate, 0.03, from: t0.addingTimeInterval(0.4), seconds: 1.5)  // "no, wait", muttered
        gate.partial("Yes. No, wait.", at: t0.addingTimeInterval(1.7))
        XCTAssertEqual(gate.kept, "Yes.")
        XCTAssertNil(gate.cut(text: "Yes. No, wait.", now: t0.addingTimeInterval(2.0)))
        // A bare yes is never the kept part, whatever follows.
        var bare = TailGate()
        feed(&bare, 0.10, from: t0, seconds: 0.4)
        bare.partial("Jarvis, yes.", at: t0.addingTimeInterval(0.6))
        feed(&bare, 0.03, from: t0.addingTimeInterval(0.4), seconds: 1.5)
        bare.partial("Jarvis, yes. What are you people", at: t0.addingTimeInterval(1.7))
        XCTAssertNil(bare.cut(text: "Jarvis, yes. What are you people", now: t0.addingTimeInterval(2.0)))
        // Retract words in the cut-off part keep a normal request whole too.
        var req = TailGate()
        feed(&req, 0.12, from: t0, seconds: 1.5)
        req.partial("Highlight the changes.", at: t0.addingTimeInterval(1.8))
        feed(&req, 0.03, from: t0.addingTimeInterval(1.5), seconds: 3)
        req.partial("Highlight the changes. Don't.", at: t0.addingTimeInterval(3.2))
        XCTAssertNil(req.cut(text: "Highlight the changes. Don't.", now: t0.addingTimeInterval(3.05)))
    }

    // Round 9 (security): a kept part that alone reads as a Confirm answer is never the cut.
    private func answerThenRoom(_ answer: String, _ room: String) -> String? {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.10, from: t0, seconds: 0.6)                          // the answer
        gate.partial(answer, at: t0.addingTimeInterval(0.8))
        feed(&gate, 0.03, from: t0.addingTimeInterval(0.6), seconds: 2.5)  // quieter, 1.5 s+ later
        let full = answer + " " + room
        gate.partial(full, at: t0.addingTimeInterval(2.4))
        XCTAssertEqual(gate.kept, answer)
        return gate.cut(text: full, now: t0.addingTimeInterval(2.6))
    }

    func testAPoliteYesIsNeverCut() {
        XCTAssertNil(answerThenRoom("Yes, thank you.", "actually only the docs"))
        XCTAssertNil(answerThenRoom("Confirmed, thanks.", "But just the readme"))
        XCTAssertNil(answerThenRoom("Jarvis, yes, thank you.", "actually only on the test branch"))
        XCTAssertNil(answerThenRoom("Hey, John Pierce. Yes, thanks.", "only the docs"))  // misheard wake
        XCTAssertNil(answerThenRoom("No thanks.", "well maybe the readme"))
        XCTAssertNil(answerThenRoom("Never mind, sir.", "just the docs then"))
        // A real request still gets its room words cut.
        XCTAssertEqual(answerThenRoom("Highlight the changes.", "What are you people"), "Highlight the changes.")
    }

    func testAnIncompleteRequestIsNeverCut() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.12, from: t0, seconds: 1.0)
        gate.partial("show me the last", at: t0.addingTimeInterval(1.1))
        feed(&gate, 0.03, from: t0.addingTimeInterval(1.0), seconds: 3)
        gate.partial("show me the last thing they said", at: t0.addingTimeInterval(3.5))
        XCTAssertNil(gate.kept)
        XCTAssertNil(gate.cut(text: "show me the last thing they said", now: t0.addingTimeInterval(4)))
        XCTAssertEqual(TailGate(ratio: 7).ratio, TailGate.defaultRatio)  // a bad service value is ignored
    }

    func testLevelBlocksAreRMSPer100ms() {
        let t0 = Date()
        let samples = [LevelSample(at: t0.addingTimeInterval(0.01), rms: 0.1, frames: 1024),
                       LevelSample(at: t0.addingTimeInterval(0.05), rms: 0.3, frames: 1024),
                       LevelSample(at: t0.addingTimeInterval(0.25), rms: 0.02, frames: 1024),
                       LevelSample(at: t0.addingTimeInterval(-0.5), rms: 0.9, frames: 1024)]  // before `from`
        let blocks = LevelBlocks.blocks(samples, from: t0, to: t0.addingTimeInterval(0.3))
        XCTAssertEqual(blocks.count, 3)
        XCTAssertEqual(blocks[0], 0.2236, accuracy: 0.0001)  // sqrt((0.01 + 0.09) / 2)
        XCTAssertEqual(blocks[1], 0)
        XCTAssertEqual(blocks[2], 0.02, accuracy: 0.0001)
        XCTAssertEqual(LevelBlocks.blocks(samples, from: t0, to: t0), [])
    }
}
