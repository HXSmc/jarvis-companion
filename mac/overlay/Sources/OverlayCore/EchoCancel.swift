import Foundation

/// Pure decisions for the echo canceller (voice processing on the app's mic engine).
public enum EchoCancel {
    /// What a wanted engine change does now: nothing to do, rebuild the engine at once, or wait for idle.
    public enum Reconfigure: Equatable { case none, now, later }
    /// What a raw wake hit does: ignore it with a reason, or hand it to the wake gate.
    public enum WakeAction: Equatable { case ignore(reason: String), gate }

    /// The canceller is wanted only in .aec mode, on speakers, with no Bluetooth mic input.
    public static func wantOn(aecMode: Bool, outputHeadphones: Bool, inputBluetooth: Bool) -> Bool {
        aecMode && !outputHeadphones && !inputBluetooth
    }

    /// Barge-in is allowed right now: .aec on headphones or with the canceller really running
    /// (a failed reconfigure leaves it off on speakers), .headphones on a headphones route, .off never.
    public static func bargeInEnabled(mode: String, headphones: Bool, active: Bool) -> Bool {
        switch mode {
        case "aec": return headphones || active
        case "headphones": return headphones
        default: return false
        }
    }

    /// An engine that stopped by itself rebuilds now even when busy; no want/active difference is
    /// nothing; a difference rebuilds at once when idle, else waits for the next idle moment.
    public static func reconfigure(want: Bool, active: Bool, idle: Bool, engineStopped: Bool) -> Reconfigure {
        if engineStopped { return .now }
        if want == active { return .none }
        return idle ? .now : .later
    }

    /// A raw wake hit while Jarvis's own audio plays through the canceller is that audio's residual,
    /// never the owner: ignored while the canceller runs and the barge listener has no turn of its
    /// own; an open barge turn is ignored too (its words already flow there); the rest reaches the
    /// wake gate.
    public static func wakeAction(audioBusy: Bool, streaming: Bool, echoCancelled: Bool,
                                  bargeListening: Bool, bargeTurnOpen: Bool) -> WakeAction {
        if bargeTurnOpen { return .ignore(reason: "barge_turn") }
        if (audioBusy || streaming) && echoCancelled && bargeListening { return .ignore(reason: "echo_cancel") }
        return .gate
    }

    /// A barge run lives this long at the earliest before a restart (a recogniser that fails at once
    /// must not restart every tick; it restarts at most once per `minBargeRunS`).
    public static let minBargeRunS: TimeInterval = 1.0

    /// When to restart the barge run: nil never, 0 now, else one retry after that many seconds. It
    /// restarts when it ended unconfirmed (a reconfigure cancelled it, or the recogniser gave up),
    /// no other turn listens and its reply still plays; `ranFor` counts from the run's creation. A
    /// run younger than `minBargeRunS` gets one delayed retry (`retried`: already queued), so it is
    /// never left dead until the next clip. Else `wakeAction` sees no listener for the rest of the
    /// reply and a raw self-hit reaches the gate.
    public static func restartBargeRun(confirmed: Bool, anyTurnOpen: Bool, audioBusy: Bool,
                                       ranFor: TimeInterval, retried: Bool) -> TimeInterval? {
        guard !confirmed && !anyTurnOpen && audioBusy else { return nil }
        if ranFor >= minBargeRunS { return 0 }
        return retried ? nil : minBargeRunS - ranFor
    }

    /// The engine is stopped when it started once but does not run now: a configuration change
    /// stopped it, or a reconfigure threw (no input for a moment during a device switch). The next
    /// configuration or route change then rebuilds it instead of leaving the mic dead.
    public static func engineStopped(startedOnce: Bool, running: Bool) -> Bool {
        startedOnce && !running
    }

    /// An input format the tap can use, with or without voice processing (Mac run 1: the MacBook Air
    /// mic is a 3-channel array; voice processing keeps the node multi-channel). Else `failed reason=format`.
    public static func formatUsable(rate: Double, channels: Int) -> Bool {
        rate > 0 && channels > 0
    }

    /// Which mono signal levels, the ring and the recogniser get from the tap (the `src=` field):
    /// channel 0 of a multi-channel node (the voice processor's output is 1 channel, app log
    /// `theActualVPOutputFmt = 1 ch`), the only channel of a mono one.
    public static func monoSource(channels: Int) -> String {
        channels > 1 ? "ch0" : "mono"
    }

    /// The playback makeup gain for what the canceller takes off the mic: `gainDb` of gain, capped
    /// so a block that already peaks at `peak` lands at 0.98 (no clipping), never below 1.
    public static func makeupGain(peak: Float, gainDb: Double) -> Float {
        let g = Float(pow(10, gainDb / 20))
        guard peak > 0 else { return g }
        return max(1, min(g, 0.98 / peak))
    }
}
