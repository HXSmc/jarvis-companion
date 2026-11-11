import Foundation

/// GET /overlay/events (server-sent events), one line at a time: an `event:` line names the event,
/// the `data:` line after it completes it. Comments (": ping") and blank lines are ignored.
public struct SSELines {
    private var name = ""

    public init() {}

    public mutating func feed(_ line: String) -> (name: String, data: String)? {
        if line.hasPrefix("event:") {
            name = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
            return nil
        }
        guard line.hasPrefix("data:"), !name.isEmpty else { return nil }
        defer { name = "" }
        return (name, line.dropFirst(5).trimmingCharacters(in: .whitespaces))
    }
}

extension OverlayEvent {
    /// A `hud` event the service pushes outside any turn (a confirmation that expired after its turn
    /// ended). Its card is always shown (it replaces the Confirm card by id); its spoken line only
    /// when Jarvis is idle, never over a reply or an open mic. Anything else is ignored.
    public func pushed(idle: Bool) -> OverlayEvent? {
        switch self {
        case .card: return self
        case .say, .audio: return idle ? self : nil
        default: return nil
        }
    }
}
