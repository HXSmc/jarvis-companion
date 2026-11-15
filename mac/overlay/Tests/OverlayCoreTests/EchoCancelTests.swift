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

    /// Voice processing on: the output side runs at the input node's rate (Mac run b: 48000 Hz in,
    /// 44100 Hz out failed with -10875), output channels kept to 1...2; no rate or no channel refuses.
    func testVoiceOutputFormat() {
        let f = EchoCancel.voiceOutputFormat(inputRate: 48000, inputChannels: 5, outputChannels: 1)
        XCTAssertEqual(f?.rate, 48000)
        XCTAssertEqual(f?.channels, 1)
        XCTAssertEqual(EchoCancel.voiceOutputFormat(inputRate: 44100, inputChannels: 1, outputChannels: 2)?.channels, 2)
        XCTAssertEqual(EchoCancel.voiceOutputFormat(inputRate: 48000, inputChannels: 3, outputChannels: 0)?.channels, 1)
        XCTAssertEqual(EchoCancel.voiceOutputFormat(inputRate: 48000, inputChannels: 3, outputChannels: 6)?.channels, 2)
        XCTAssertNil(EchoCancel.voiceOutputFormat(inputRate: 48000, inputChannels: 0, outputChannels: 1))
        XCTAssertNil(EchoCancel.voiceOutputFormat(inputRate: 0, inputChannels: 5, outputChannels: 1))
    }

    /// dBFS: full scale 0.0, half -6.0, silence (and -inf) -120.0, one decimal in the rows.
    func testDbfs() {
        XCTAssertEqual(EchoCancel.dbfs(1), 0, accuracy: 1e-9)
        XCTAssertEqual(EchoCancel.dbfs(0.5), -6.02, accuracy: 0.01)
        XCTAssertEqual(EchoCancel.dbfs(0), -120)
        XCTAssertEqual(EchoCancel.dbfs(1e-9), -120)  // below the floor
        XCTAssertEqual(EchoCancel.dbText(0.001), "-60.0")
        XCTAssertEqual(EchoCancel.dbText(0), "-120.0")
    }

    /// The zero check is exact: one tiny sample is not silence; interleaved data steps by the stride.
    func testChannelStatsZeroCheck() {
        let zeros = [Float](repeating: 0, count: 8)
        zeros.withUnsafeBufferPointer { XCTAssertTrue(EchoCancel.channelStats($0.baseAddress!, frames: 8, stride: 1).zero) }
        var tiny = zeros
        tiny[5] = 1e-7
        tiny.withUnsafeBufferPointer { XCTAssertFalse(EchoCancel.channelStats($0.baseAddress!, frames: 8, stride: 1).zero) }
        // interleaved 2 ch: channel 0 = 0.0, channel 1 = -0.5
        let inter: [Float] = [0, -0.5, 0, -0.5, 0, -0.5]
        inter.withUnsafeBufferPointer { b in
            let c0 = EchoCancel.channelStats(b.baseAddress!, frames: 3, stride: 2)
            XCTAssertTrue(c0.zero)
            let c1 = EchoCancel.channelStats(b.baseAddress! + 1, frames: 3, stride: 2)
            XCTAssertFalse(c1.zero)
            XCTAssertEqual(c1.peak, 0.5, accuracy: 1e-9)
            XCTAssertEqual(c1.sumSq, 0.75, accuracy: 1e-9)
        }
    }

    /// The mic_signal row: per-channel peak and RMS in dBFS, frames, ch0_zero over every buffer.
    func testMicSignalDetail() {
        var s = EchoCancel.MicSignal()
        s.add(peak: [0, 0.5], sumSq: [0, 0.25 * 100], frames: 100, ch0Zero: true)
        s.add(peak: [0, 0.1], sumSq: [0, 0.25 * 100], frames: 100, ch0Zero: true)
        XCTAssertEqual(s.detail, "frames=200 ch0_zero=yes peak_db=-120.0,-6.0 rms_db=-120.0,-6.0")
        s.add(peak: [0.001, 0], sumSq: [0, 0], frames: 100, ch0Zero: false)
        XCTAssertTrue(s.detail.hasPrefix("frames=300 ch0_zero=no peak_db=-60.0,-6.0 "))
        XCTAssertEqual(s.peakDb(0), -60, accuracy: 0.01)
        XCTAssertEqual(EchoCancel.MicSignal().detail, "frames=0 ch0_zero=no peak_db= rms_db=")
    }

    /// Channel 0 exact silence for 1 s while another channel is live moves the source to the lowest
    /// live channel, once; a reply playing (hold), a quiet other channel or a short gap do not.
    func testSourcePick() {
        var p = EchoCancel.SourcePick()
        XCTAssertNil(p.step(ch0Zero: true, rmsDb: [-120, -120, -60], seconds: 0.6, hold: false))
        XCTAssertNil(p.step(ch0Zero: false, rmsDb: [-80, -120, -60], seconds: 0.1, hold: false))  // gap resets
        XCTAssertNil(p.step(ch0Zero: true, rmsDb: [-120, -120, -60], seconds: 0.6, hold: false))
        XCTAssertEqual(p.step(ch0Zero: true, rmsDb: [-120, -95, -60, -50], seconds: 0.5, hold: false), 2)
        XCTAssertEqual(p.channel, 2)
        XCTAssertNil(p.step(ch0Zero: true, rmsDb: [-120, -60, -60], seconds: 5, hold: false))  // moves once
        var q = EchoCancel.SourcePick()
        XCTAssertNil(q.step(ch0Zero: true, rmsDb: [-120, -60], seconds: 2, hold: true))  // a reply plays
        XCTAssertNil(q.step(ch0Zero: true, rmsDb: [-120, -95], seconds: 2, hold: false))  // nothing live
        XCTAssertEqual(q.channel, 0)
        var mono = EchoCancel.SourcePick()
        XCTAssertNil(mono.step(ch0Zero: true, rmsDb: [-120], seconds: 2, hold: false))  // a mono node
        XCTAssertEqual(EchoCancel.monoSource(channels: 5, channel: 2), "ch2")
    }

    /// Two wake turns in a row that heard nothing with the processed channel below -70 dBFS trip the
    /// guard; a quiet real voice (above -70), a heard turn in between, or STT results never do.
    func testNoSignalGuard() {
        var g = EchoCancel.NoSignalGuard()
        XCTAssertFalse(g.wakeTurnEnded(heardNothing: true, sttResults: 0, peakDb: -95))
        XCTAssertTrue(g.wakeTurnEnded(heardNothing: true, sttResults: 0, peakDb: -120))
        var quiet = EchoCancel.NoSignalGuard()
        XCTAssertFalse(quiet.wakeTurnEnded(heardNothing: true, sttResults: 0, peakDb: -60))
        XCTAssertFalse(quiet.wakeTurnEnded(heardNothing: true, sttResults: 0, peakDb: -65))  // quiet voice
        var reset = EchoCancel.NoSignalGuard()
        XCTAssertFalse(reset.wakeTurnEnded(heardNothing: true, sttResults: 0, peakDb: -100))
        XCTAssertFalse(reset.wakeTurnEnded(heardNothing: false, sttResults: 3, peakDb: -30))  // heard
        XCTAssertFalse(reset.wakeTurnEnded(heardNothing: true, sttResults: 0, peakDb: -100))
        var stt = EchoCancel.NoSignalGuard()
        XCTAssertFalse(stt.wakeTurnEnded(heardNothing: true, sttResults: 1, peakDb: -100))
        XCTAssertFalse(stt.wakeTurnEnded(heardNothing: true, sttResults: 1, peakDb: -100))
    }
}
