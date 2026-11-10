import AppKit
import OverlayCore

/// Moves anchored shapes with their windows. Polls the CG window list on a utility queue
/// (no AX calls, window titles never read, nothing leaves the process) and hands each
/// change to the main actor. Exists only while the model holds an anchored shape.
final class WindowFollower {
    typealias Window = (id: UInt32, pid: Int32, frame: CGRect)

    private let queue: DispatchQueue
    private let tracker: ShapeFollowTracker

    init(apply: @escaping @MainActor (FollowUpdate) -> Void) {
        let queue = DispatchQueue(label: "jarvis.overlay.window-follow", qos: .utility)
        self.queue = queue
        tracker = ShapeFollowTracker(source: CGWindowSource(),
                                     scheduler: DispatchTickScheduler(queue: queue),
                                     screens: [],
                                     onUpdate: { update in
            // FIFO hop: the last update of a drag must land last.
            DispatchQueue.main.async { MainActor.assumeIsolated { apply(update) } }
        })
    }

    /// Full anchored set (key = model shape key); empty stops the timer.
    @MainActor func setAnchors(_ anchors: [Int: ShapeAnchor]) {
        let screens = [Self.panelScreen()]
        queue.async { [tracker] in
            tracker.screens = screens
            tracker.setAnchors(anchors)
        }
    }

    /// Creation hit-test for one add-batch: one window-list call. Call off the main actor.
    static func anchors(for rects: [Int: CGRect]) -> [Int: ShapeAnchor] {
        let windows = normalWindows()
        return rects.compactMapValues { ShapeFollow.anchor(rect: $0, windows: windows) }
    }

    /// On-screen normal windows (layer 0, visible) of other apps, front to back.
    static func normalWindows() -> [Window] {
        let own = ProcessInfo.processInfo.processIdentifier
        let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                              kCGNullWindowID) as? [[String: Any]] ?? []
        return list.compactMap { info -> Window? in
            guard (info[kCGWindowLayer as String] as? Int) == 0,
                  let pid = info[kCGWindowOwnerPID as String] as? Int32, pid != own,
                  (info[kCGWindowAlpha as String] as? Double ?? 1) > 0,
                  let id = (info[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let bounds = info[kCGWindowBounds as String] as? NSDictionary,
                  let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary)
            else { return nil }
            return (id, pid, frame)
        }
    }

    /// The overlay panel's display (NSScreen.main, as OverlayPanel uses) in global top-left
    /// points: flipped against the origin display, not the main one.
    @MainActor private static func panelScreen() -> CGRect {
        guard let frame = NSScreen.main?.frame, let originHeight = NSScreen.screens.first?.frame.height
        else { return .zero }
        return CGRect(x: frame.minX, y: originHeight - frame.maxY, width: frame.width, height: frame.height)
    }
}

/// One on-screen window list per tick; a per-id description only for anchored ids missing
/// from it (minimized / hidden / other Space vs closed).
private struct CGWindowSource: WindowSource {
    func sample(ids: [UInt32]) -> [UInt32: WindowSample] {
        let wanted = Set(ids)
        let windows = WindowFollower.normalWindows()
        var out: [UInt32: WindowSample] = [:]
        for (index, window) in windows.enumerated() where wanted.contains(window.id) {
            out[window.id] = WindowSample(pid: window.pid, frame: window.frame, isOnscreen: true,
                                          above: windows[..<index].map { $0.frame })
        }
        var missing = ids.filter { out[$0] == nil }.map { UnsafeRawPointer(bitPattern: UInt($0)) }
        guard !missing.isEmpty else { return out }
        // The API takes a CFArray of raw CGWindowID values, not CFNumbers.
        let array = missing.withUnsafeMutableBufferPointer { CFArrayCreate(nil, $0.baseAddress, $0.count, nil) }
        let infos = CGWindowListCreateDescriptionFromArray(array) as? [[String: Any]] ?? []
        for info in infos {
            guard let id = (info[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let pid = info[kCGWindowOwnerPID as String] as? Int32,
                  let bounds = info[kCGWindowBounds as String] as? NSDictionary,
                  let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary)
            else { continue }
            out[id] = WindowSample(pid: pid, frame: frame,
                                   isOnscreen: info[kCGWindowIsOnscreen as String] as? Bool ?? false)
        }
        return out
    }
}

/// DispatchSourceTimer on the follower's serial queue (utility QoS, 5 ms leeway).
/// start() leaves it unscheduled until the tracker calls setRate(hz:).
private final class DispatchTickScheduler: TickScheduler {
    private let queue: DispatchQueue
    private var timer: DispatchSourceTimer?

    init(queue: DispatchQueue) { self.queue = queue }

    func start(_ tick: @escaping () -> Void) {
        timer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.setEventHandler(handler: tick)
        timer.resume()
        self.timer = timer
    }

    func setRate(hz: Double) {
        timer?.schedule(deadline: .now() + 1 / hz, repeating: 1 / hz, leeway: .milliseconds(5))
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }
}
