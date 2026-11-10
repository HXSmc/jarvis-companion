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

    func testVoiceActivityTracksTheNoiseFloor() {
        var vad = VoiceActivity()
        for _ in 0..<100 { XCTAssertFalse(vad.isVoiced(rms: 0.002)) }  // quiet room
        XCTAssertTrue(vad.isVoiced(rms: 0.05))                          // speech
        for _ in 0..<50 { _ = vad.isVoiced(rms: 0.05) }
        XCTAssertTrue(vad.isVoiced(rms: 0.05))  // 1 s of speech does not become the floor
        XCTAssertFalse(vad.isVoiced(rms: 0.003))
    }
}
