import Foundation

/// Decides when "my service is not running" may be said: only after the service stayed
/// unreachable through `grace` seconds of retries. A reinstall, a service restart or a Mac
/// wake drops the connection for a few seconds; that is not an outage.
public struct OutageGate {
    public let grace: TimeInterval
    public private(set) var since: Date?
    public private(set) var announced = false

    public init(grace: TimeInterval = 30) { self.grace = grace }

    /// A failed reach at `now`; true exactly once, when the outage should be announced.
    public mutating func failed(at now: Date) -> Bool {
        let start = since ?? now
        since = start
        guard !announced, now.timeIntervalSince(start) >= grace else { return false }
        announced = true
        return true
    }

    /// The service answered; true when an announced outage just ended.
    public mutating func reached() -> Bool {
        defer { since = nil; announced = false }
        return announced
    }
}
