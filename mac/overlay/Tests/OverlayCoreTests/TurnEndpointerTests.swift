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
}
