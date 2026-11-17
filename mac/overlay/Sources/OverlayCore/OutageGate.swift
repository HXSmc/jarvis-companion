import Foundation

/// Decides when "my service is not running" may be said: only after the service stayed
/// unreachable through `grace` seconds of retries. A reinstall, a service restart or a Mac
/// wake drops the connection for a few seconds; that is not an outage.
public struct OutageGate {
    public let grace: TimeInterval
    /// Two failures further apart than this were not one retry streak (the Mac slept, or the
    /// loop was paused): the grace starts again. nil = off.
    public let maxGap: TimeInterval?
    public private(set) var since: Date?
    public private(set) var last: Date?
    public private(set) var announced = false

    public init(grace: TimeInterval = 30, maxGap: TimeInterval? = nil) {
        self.grace = grace
        self.maxGap = maxGap
    }

    /// A failed reach at `now`; true exactly once, when the outage should be announced.
    public mutating func failed(at now: Date) -> Bool {
        if let maxGap, let last, !announced, now.timeIntervalSince(last) > maxGap { since = now }
        let start = since ?? now
        since = start
        last = now
        guard !announced, now.timeIntervalSince(start) >= grace else { return false }
        announced = true
        return true
    }

    /// The service answered; true when an announced outage just ended.
    public mutating func reached() -> Bool {
        defer { since = nil; last = nil; announced = false }
        return announced
    }

    /// A fixed word when `error` means the service DID answer but its reply did not fit (a
    /// decode error). That is not an outage: it never counts toward the "not running" line.
    /// nil = no answer at all.
    public static func answeredNote(_ error: Error) -> String? {
        error is DecodingError ? "decode" : nil
    }

    /// The spoken line and the banner for an announced outage. A timeout means something
    /// accepted the connection but never answered, so "not running" would be wrong.
    public static func announcement(timedOut: Bool) -> (spoken: String, banner: String) {
        let state = timedOut ? "is not answering" : "is not running"
        return ("My service \(state). I've put the fix on screen.",
                "Jarvis service \(state). Fix: run scripts/install-mac-overlay.sh again. "
                    + "Details: ~/Library/Logs/JarvisOverlay/service.log")
    }
}
