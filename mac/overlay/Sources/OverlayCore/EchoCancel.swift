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

    /// A barge run whose listening turn ended unconfirmed (a reconfigure cancelled it, or the
    /// recogniser gave up) while its reply plays on: drop it so the next clip starts a fresh run,
    /// else `wakeAction` sees no listener and a raw self-hit reaches the wake gate.
    public static func bargeRunEnded(confirmed: Bool, listening: Bool, turnOpen: Bool) -> Bool {
        !confirmed && !listening && !turnOpen
    }

    /// The playback makeup gain for what the canceller takes off the mic: `gainDb` of gain, capped
    /// so a block that already peaks at `peak` lands at 0.98 (no clipping), never below 1.
    public static func makeupGain(peak: Float, gainDb: Double) -> Float {
        let g = Float(pow(10, gainDb / 20))
        guard peak > 0 else { return g }
        return max(1, min(g, 0.98 / peak))
    }
}
