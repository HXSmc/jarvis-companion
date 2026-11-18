import Foundation
import XCTest
@testable import OverlayCore

final class OutageGateTests: XCTestCase {
    func testShortDropIsNotAnOutage() {
        var gate = OutageGate(grace: 30)
        let t0 = Date()
        XCTAssertFalse(gate.failed(at: t0))                       // first failure: retry, stay quiet
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(8)))  // service restarting
        XCTAssertFalse(gate.reached())                            // back: nothing was announced
        XCTAssertNil(gate.since)
    }

    func testAnnouncedOnceAfterGraceThenCleared() {
        var gate = OutageGate(grace: 30)
        let t0 = Date()
        XCTAssertFalse(gate.failed(at: t0))
        XCTAssertTrue(gate.failed(at: t0.addingTimeInterval(31)))
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(45)))  // said only once
        XCTAssertTrue(gate.reached())
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(50)))  // a new outage starts its own grace
    }

    // jfollow10i item B (ii): a successful utterance turn during the grace resets the gate.
    func testUtteranceSuccessDuringGraceIsNoAnnounce() {
        var gate = OutageGate(grace: 30, maxGap: 10)
        let t0 = Date()
        XCTAssertFalse(gate.failed(at: t0))                        // health fails...
        XCTAssertFalse(gate.reached())                             // ...but a turn's reply arrived
        for s in stride(from: 3.0, through: 30.0, by: 3.0) {       // health keeps failing for 30 s
            XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(s)))
        }
        XCTAssertFalse(gate.announced)                             // only 27 s since the reset
    }

    // (iii): a reply that does not decode is an answer, never the "not running" line.
    func testDecodeErrorIsAnAnswerNotAnOutage() {
        let decode = DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "bad"))
        XCTAssertEqual(OutageGate.answeredNote(decode), "decode")
        XCTAssertNil(OutageGate.answeredNote(URLError(.cannotConnectToHost)))  // a stopped service
        XCTAssertNil(OutageGate.answeredNote(URLError(.timedOut)))
    }

    // Regression (cause c, Mac sleep): one failure before sleep and one after wake are hours
    // apart on the wall clock, but that is not 30 s of retrying. Before: announced at once.
    func testSleepGapRestartsTheGrace() {
        var gate = OutageGate(grace: 30, maxGap: 10)
        let t0 = Date()
        XCTAssertFalse(gate.failed(at: t0))                            // stream dropped as the Mac slept
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(3600)))   // first retry after wake
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(3603)))
        XCTAssertFalse(gate.announced)
        var off = OutageGate(grace: 30)                                // maxGap off = the old gate
        XCTAssertFalse(off.failed(at: t0))
        XCTAssertTrue(off.failed(at: t0.addingTimeInterval(3600)))
    }

    // 24: a stopped service retried every ~3 s is still announced once after the grace.
    func testRealOutageWithRetriesStillAnnouncedOnce() {
        var gate = OutageGate(grace: 30, maxGap: 10)
        let t0 = Date()
        var said = 0
        for s in stride(from: 0.0, through: 45.0, by: 3.0) where gate.failed(at: t0.addingTimeInterval(s)) {
            said += 1
        }
        XCTAssertEqual(said, 1)
        XCTAssertTrue(gate.reached())                                  // back: the banner clears
    }

    // (iv): the banner names the real failure.
    func testAnnouncementMatchesTheFailure() {
        XCTAssertEqual(OutageGate.announcement(timedOut: false).spoken,
                       "My service is not running. I've put the fix on screen.")
        XCTAssertTrue(OutageGate.announcement(timedOut: false).banner.hasPrefix("Jarvis service is not running."))
        XCTAssertTrue(OutageGate.announcement(timedOut: true).banner.hasPrefix("Jarvis service is not answering."))
        XCTAssertTrue(OutageGate.announcement(timedOut: true).spoken.contains("not answering"))
    }

    // Regression (cause b, proven live in jfollow10i round 3): a restart must not be said. jfollow11b
    // opens the port before the voice warms up, so a restart refuses for a few seconds; 20 s is a
    // slow one. A 45 s stop plus that start-up is still said, once.
    func testServiceRestartWindowIsNotAnOutage() {
        let t0 = Date()
        var restart = OutageGate.service                                // kickstart: refused 20 s
        for s in stride(from: 0.0, through: 20.0, by: 3.0) {
            XCTAssertFalse(restart.failed(at: t0.addingTimeInterval(s)), "announced at \(s) s")
        }
        XCTAssertFalse(restart.reached())                               // back: nothing was said
        var stopped = OutageGate.service                                // 45 s stop + start-up
        var said = 0
        for s in stride(from: 0.0, through: 60.0, by: 3.0) where stopped.failed(at: t0.addingTimeInterval(s)) {
            said += 1
        }
        XCTAssertEqual(said, 1)                                         // a real stop is still said once
    }

    // jfollow11b criterion 18: the app's gate is quiet at grace - 1 s and speaks at grace, once.
    func testServiceGateAnnouncesAtGraceExactlyOnce() {
        let t0 = Date()
        var gate = OutageGate.service
        let grace = gate.grace
        var s = 0.0
        while s < grace - 1 {                                           // retries 2 s apart, under maxGap
            XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(s)), "announced at \(s) s")
            s += 2
        }
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(grace - 1)))
        XCTAssertTrue(gate.failed(at: t0.addingTimeInterval(grace)))
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(grace + 2)))  // said once
    }
}
