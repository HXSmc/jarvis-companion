import Foundation
import XCTest
@testable import OverlayCore

final class EchoCancelTests: XCTestCase {
    /// aec on speakers wants the canceller; a headphones route, a Bluetooth mic input or a non-aec
    /// mode never does.
    func testWantOnTable() {
        XCTAssertTrue(EchoCancel.wantOn(aecMode: true, outputHeadphones: false, inputBluetooth: false))
        XCTAssertFalse(EchoCancel.wantOn(aecMode: true, outputHeadphones: true, inputBluetooth: false))
        XCTAssertFalse(EchoCancel.wantOn(aecMode: true, outputHeadphones: false, inputBluetooth: true))
        XCTAssertFalse(EchoCancel.wantOn(aecMode: false, outputHeadphones: false, inputBluetooth: false))  // headphones mode
        XCTAssertFalse(EchoCancel.wantOn(aecMode: false, outputHeadphones: true, inputBluetooth: false))  // off
    }

    /// .off never allows barge-in; .headphones only on a headphones route; .aec on headphones or
    /// with the canceller really running (a failed reconfigure leaves it off on speakers).
    func testBargeInEnabledTable() {
        XCTAssertFalse(EchoCancel.bargeInEnabled(mode: "off", headphones: true, active: true))
        XCTAssertFalse(EchoCancel.bargeInEnabled(mode: "off", headphones: true, active: false))
        XCTAssertFalse(EchoCancel.bargeInEnabled(mode: "off", headphones: false, active: true))
        XCTAssertFalse(EchoCancel.bargeInEnabled(mode: "off", headphones: false, active: false))
        XCTAssertTrue(EchoCancel.bargeInEnabled(mode: "headphones", headphones: true, active: false))
        XCTAssertFalse(EchoCancel.bargeInEnabled(mode: "headphones", headphones: false, active: true))
        XCTAssertTrue(EchoCancel.bargeInEnabled(mode: "aec", headphones: true, active: false))
        XCTAssertTrue(EchoCancel.bargeInEnabled(mode: "aec", headphones: false, active: true))
        XCTAssertFalse(EchoCancel.bargeInEnabled(mode: "aec", headphones: false, active: false))
    }

    /// An engine that stopped by itself rebuilds now even when busy; a want/active difference goes
    /// now when idle and waits when busy; no difference is nothing.
    func testReconfigureTable() {
        XCTAssertEqual(EchoCancel.reconfigure(want: true, active: true, idle: true, engineStopped: false), .none)
        XCTAssertEqual(EchoCancel.reconfigure(want: true, active: false, idle: false, engineStopped: true), .now)
        XCTAssertEqual(EchoCancel.reconfigure(want: true, active: true, idle: false, engineStopped: true), .now)
        XCTAssertEqual(EchoCancel.reconfigure(want: true, active: false, idle: true, engineStopped: false), .now)
        XCTAssertEqual(EchoCancel.reconfigure(want: true, active: false, idle: false, engineStopped: false), .later)
    }

    /// Raw wake hits over echo-cancelled playback are the reply's own residual and only cut through
    /// a barge run; an open barge turn swallows them; everything else reaches the wake gate.
    func testWakeActionTable() {
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: true, streaming: true, echoCancelled: true,
                                             bargeListening: true, bargeTurnOpen: false),
                       .ignore(reason: "echo_cancel"))  // playing
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: false, streaming: false, echoCancelled: true,
                                             bargeListening: true, bargeTurnOpen: true),
                       .ignore(reason: "barge_turn"))  // audio stopped, a barge turn is open
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: true, streaming: false, echoCancelled: true,
                                             bargeListening: false, bargeTurnOpen: false),
                       .gate)  // playing, no barge run
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: true, streaming: false, echoCancelled: false,
                                             bargeListening: true, bargeTurnOpen: false),
                       .gate)  // playing, canceller failed/off
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: true, streaming: false, echoCancelled: false,
                                             bargeListening: true, bargeTurnOpen: false),
                       .gate)  // headphones route, canceller never started
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: false, streaming: false, echoCancelled: false,
                                             bargeListening: false, bargeTurnOpen: false),
                       .gate)  // idle
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: false, streaming: true, echoCancelled: true,
                                             bargeListening: false, bargeTurnOpen: false),
                       .gate)  // stream open, no audio playing yet
        XCTAssertEqual(EchoCancel.wakeAction(audioBusy: false, streaming: true, echoCancelled: true,
                                             bargeListening: true, bargeTurnOpen: false),
                       .ignore(reason: "echo_cancel"))  // a gap between clips inside a reply
    }

    /// The 6 dB makeup gain with its clip cap: 0.7 peak hits the 0.98/peak cap, 1.0 gains nothing,
    /// an unknown peak takes the plain gain.
    func testMakeupGain() {
        XCTAssertEqual(EchoCancel.makeupGain(peak: 0.25, gainDb: 6), 1.995, accuracy: 0.001)
        XCTAssertEqual(EchoCancel.makeupGain(peak: 0.7, gainDb: 6), 0.98 / 0.7, accuracy: 0.0001)
        XCTAssertEqual(EchoCancel.makeupGain(peak: 1.0, gainDb: 6), 1.0)
        XCTAssertEqual(EchoCancel.makeupGain(peak: 0, gainDb: 6), 1.995, accuracy: 0.001)
    }

    /// I1 (hub ruling): a barge run that ended unconfirmed (a reconfigure cancelled it, or the
    /// recogniser gave up) while its reply plays on restarts, so raw self-hits stay ignored; a
    /// confirmed run, another open turn, a drained reply or a run that failed at once does not.
    func testRestartBargeRunTable() {
        // nil: no restart; 0: restart now; > 0: one retry after that many seconds.
        XCTAssertEqual(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: false, audioBusy: true, ranFor: 5, retried: false), 0)
        XCTAssertEqual(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: false, audioBusy: true, ranFor: 1.0, retried: false), 0)
        XCTAssertEqual(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: false, audioBusy: true, ranFor: 5, retried: true), 0)
        XCTAssertNil(EchoCancel.restartBargeRun(confirmed: true, anyTurnOpen: false, audioBusy: true, ranFor: 5, retried: false))
        XCTAssertNil(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: true, audioBusy: true, ranFor: 5, retried: false))
        XCTAssertNil(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: false, audioBusy: false, ranFor: 5, retried: false))
        // r4 finding 3: a run that died young (no gate block yet, ranFor from its creation) is
        // retried once when it reaches minBargeRunS, not left dead until the next clip.
        XCTAssertEqual(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: false, audioBusy: true, ranFor: 0.2, retried: false)!,
                       0.8, accuracy: 1e-9)
        XCTAssertEqual(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: false, audioBusy: true, ranFor: 0, retried: false),
                       EchoCancel.minBargeRunS)
        XCTAssertNil(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: false, audioBusy: true, ranFor: 0.2, retried: true))
        // A live run (its turn still open) never queues a retry.
        XCTAssertNil(EchoCancel.restartBargeRun(confirmed: false, anyTurnOpen: true, audioBusy: true, ranFor: 0.2, retried: false))
    }

    /// r4 finding 1: a reconfigure that threw leaves the engine stopped; once it started once, the
    /// next configuration/route change must see it as stopped (and rebuild), not as fine.
    func testEngineStoppedAfterFailedReconfigure() {
        XCTAssertTrue(EchoCancel.engineStopped(startedOnce: true, running: false))
        XCTAssertFalse(EchoCancel.engineStopped(startedOnce: true, running: true))
        XCTAssertFalse(EchoCancel.engineStopped(startedOnce: false, running: false))
        let stopped = EchoCancel.engineStopped(startedOnce: true, running: false)
        XCTAssertEqual(EchoCancel.reconfigure(want: false, active: false, idle: true, engineStopped: stopped), .now)
        XCTAssertEqual(EchoCancel.reconfigure(want: true, active: false, idle: false, engineStopped: stopped), .now)
    }

    /// Mac run 1: the 3-channel MacBook Air mic with voice processing on is usable (it was refused
    /// as not mono, so the canceller never ran); levels and the recogniser read channel 0.
    func testFormatUsable() {
        XCTAssertTrue(EchoCancel.formatUsable(rate: 48000, channels: 1))
        XCTAssertTrue(EchoCancel.formatUsable(rate: 48000, channels: 3))
        XCTAssertTrue(EchoCancel.formatUsable(rate: 44100, channels: 2))
        XCTAssertFalse(EchoCancel.formatUsable(rate: 0, channels: 1))
        XCTAssertFalse(EchoCancel.formatUsable(rate: 48000, channels: 0))
        XCTAssertEqual(EchoCancel.monoSource(channels: 3), "ch0")
        XCTAssertEqual(EchoCancel.monoSource(channels: 1), "mono")
    }
}
