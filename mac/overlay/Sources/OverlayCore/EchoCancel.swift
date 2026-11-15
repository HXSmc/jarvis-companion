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

    /// The mixer -> output connection while voice processing is on. The voice-processing IO unit
    /// refuses to start when its client input and output formats differ (Mac run b: input 48000 Hz,
    /// output tracked 44100 Hz, err=-10875), so the output side takes the input node's rate, with the
    /// output client's channel count kept to 1...2. nil when the input has no rate or no channel
    /// (the `failed reason=format` path).
    public static func voiceOutputFormat(inputRate: Double, inputChannels: Int,
                                         outputChannels: Int) -> (rate: Double, channels: Int)? {
        guard formatUsable(rate: inputRate, channels: inputChannels) else { return nil }
        return (inputRate, min(max(outputChannels, 1), 2))
    }

    /// Which mono signal levels, the ring and the recogniser get from the tap (the `src=` field):
    /// channel 0 of a multi-channel node (the voice processor's output is 1 channel, app log
    /// `theActualVPOutputFmt = 1 ch`), the only channel of a mono one.
    public static func monoSource(channels: Int, channel: Int = 0) -> String {
        channels > 1 ? "ch\(channel)" : "mono"
    }

    /// Full-scale level of a linear amplitude (peak or RMS) in dBFS, floored at -120 (silence, -inf).
    public static func dbfs(_ x: Double) -> Double {
        x > 0 ? max(-120, 20 * log10(x)) : -120
    }

    /// dBFS with one decimal, for the debug rows.
    public static func dbText(_ x: Double) -> String {
        String(format: "%.1f", dbfs(x))
    }

    /// Peak abs, sum of squares and "every sample exactly 0.0" of one channel of a tap buffer
    /// (`stride` 1 deinterleaved, the channel count interleaved).
    public static func channelStats(_ data: UnsafePointer<Float>, frames: Int, stride: Int)
        -> (peak: Double, sumSq: Double, zero: Bool) {
        var peak: Float = 0, sum: Double = 0
        for f in 0..<frames {
            let x = data[f * stride]
            peak = max(peak, abs(x))
            sum += Double(x * x)
        }
        return (Double(peak), sum, peak == 0)
    }

    /// The mic's per-channel signal over a stretch of tap buffers (the `mic_signal` row: numbers only).
    public struct MicSignal: Equatable {
        public var peak: [Double] = []   // linear peak abs per node channel
        public var sumSq: [Double] = []
        public var frames = 0
        public var ch0Zero = true        // every channel-0 sample so far was exactly 0.0
        public init() {}

        /// Adds one buffer's per-channel stats.
        public mutating func add(peak p: [Double], sumSq s: [Double], frames n: Int, ch0Zero z: Bool) {
            if peak.count < p.count {
                peak += Array(repeating: 0, count: p.count - peak.count)
                sumSq += Array(repeating: 0, count: p.count - sumSq.count)
            }
            for i in p.indices { peak[i] = max(peak[i], p[i]); sumSq[i] += s[i] }
            frames += n
            ch0Zero = ch0Zero && z
        }

        public func rms(_ c: Int) -> Double {
            c < sumSq.count && frames > 0 ? (sumSq[c] / Double(frames)).squareRoot() : 0
        }

        public func peakDb(_ c: Int) -> Double { dbfs(c < peak.count ? peak[c] : 0) }

        /// `frames=<n> ch0_zero=yes|no peak_db=<c0>,<c1>,... rms_db=<c0>,<c1>,...` (one decimal, -120.0 = silence).
        public var detail: String {
            "frames=\(frames) ch0_zero=\(frames > 0 && ch0Zero ? "yes" : "no") "
                + "peak_db=\(peak.map { dbText($0) }.joined(separator: ",")) "
                + "rms_db=\(peak.indices.map { dbText(rms($0)) }.joined(separator: ","))"
        }
    }

    /// Which node channel the voice-processing path hands on. Apple's docs do not say which channel of a
    /// multi-channel voice-processed input carries the processed voice; it starts at channel 0, and moves
    /// once to the lowest other channel above `liveDb` when channel 0 has been exact digital silence for
    /// `zeroS` of audio (a real processed signal is never exactly 0.0 for a second). `hold` (a reply plays)
    /// blocks the move: the other channels may be raw mics or the speaker reference.
    public struct SourcePick: Equatable {
        public static let zeroS = 1.0
        public static let liveDb = -90.0
        public private(set) var channel = 0
        private var zeroFor = 0.0  // seconds of exact-zero channel 0 in a row
        public init() {}

        /// One tap buffer: returns the new channel when it moves, else nil.
        public mutating func step(ch0Zero: Bool, rmsDb: [Double], seconds: Double, hold: Bool) -> Int? {
            guard channel == 0 else { return nil }
            zeroFor = ch0Zero && !hold ? zeroFor + seconds : 0
            guard zeroFor >= Self.zeroS,
                  let to = rmsDb.indices.first(where: { $0 > 0 && rmsDb[$0] > Self.liveDb }) else { return nil }
            channel = to
            return to
        }
    }

    /// The owner's safety net: two wake turns in a row on voice processing that heard nothing (no
    /// speech, 0 STT results) while the processed channel peaked below `quietDb` turn voice processing
    /// off for the app session. A quiet real voice (peak above `quietDb`) or any heard turn resets it.
    public struct NoSignalGuard: Equatable {
        public static let quietDb = -70.0
        public private(set) var misses = 0
        public init() {}

        /// A wake turn ended on voice processing; true = turn voice processing off now.
        public mutating func wakeTurnEnded(heardNothing: Bool, sttResults: Int, peakDb: Double) -> Bool {
            misses = heardNothing && sttResults == 0 && peakDb < Self.quietDb ? misses + 1 : 0
            return misses >= 2
        }
    }

    /// The playback makeup gain for what the canceller takes off the mic: `gainDb` of gain, capped
    /// so a block that already peaks at `peak` lands at 0.98 (no clipping), never below 1.
    public static func makeupGain(peak: Float, gainDb: Double) -> Float {
        let g = Float(pow(10, gainDb / 20))
        guard peak > 0 else { return g }
        return max(1, min(g, 0.98 / peak))
    }
}
