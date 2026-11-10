import Foundation

// Highlight boxes that follow the window they were drawn on. Pure geometry plus a polling
// tracker; the app supplies the CG window source and the timer (JarvisOverlay/WindowFollow.swift).
// All rects are global screen points, top-left origin of the main display (same space as
// HUDShape.box, AX frames and kCGWindowBounds).
// shortcut: Stage Manager thumbnails and full-screen Spaces get only the on-screen-list /
// isOnscreen rule (a window shrunk to a thumbnail may keep its box), add AX if owners hit it.

/// Where a shape sat in which window when it was drawn.
public struct ShapeAnchor: Equatable, Sendable {
    public var windowID: UInt32
    public var pid: Int32
    public var windowFrame: CGRect
    public var rect: CGRect

    public init(windowID: UInt32, pid: Int32, windowFrame: CGRect, rect: CGRect) {
        self.windowID = windowID
        self.pid = pid
        self.windowFrame = windowFrame
        self.rect = rect
    }
}

/// One anchored window as seen in one poll.
public struct WindowSample: Equatable, Sendable {
    public var pid: Int32
    public var frame: CGRect
    /// false: minimized, app hidden or on another Space (may come back).
    public var isOnscreen: Bool
    /// Frames of other normal windows in front of this one (front-to-back snapshot).
    public var above: [CGRect]

    public init(pid: Int32, frame: CGRect, isOnscreen: Bool, above: [CGRect] = []) {
        self.pid = pid
        self.frame = frame
        self.isOnscreen = isOnscreen
        self.above = above
    }
}

public enum ShapeFollow {
    /// Share of a shape's area that must stay inside its window and on a screen.
    static let minVisible: CGFloat = 0.5

    /// Shape rect for the window's current state, or nil when the shape must be hidden.
    /// The shape keeps its offset from the window's top-left corner and never scales.
    public static func place(_ anchor: ShapeAnchor, window: WindowSample?, screens: [CGRect]) -> CGRect? {
        guard let window, window.pid == anchor.pid, window.isOnscreen else { return nil }
        let rect = anchor.rect.offsetBy(dx: window.frame.minX - anchor.windowFrame.minX,
                                        dy: window.frame.minY - anchor.windowFrame.minY)
        let centre = CGPoint(x: rect.midX, y: rect.midY)
        guard visibleShare(of: rect, in: [window.frame]) >= minVisible,
              visibleShare(of: rect, in: screens) >= minVisible,
              !window.above.contains(where: { $0.contains(centre) })
        else { return nil }
        return rect
    }

    /// Anchor for a new shape: the frontmost window under the shape's centre, if it also holds
    /// at least half the shape. `windows` is front-to-back, already filtered to normal windows
    /// of other apps. nil keeps the shape static (menu bar, desktop, our own panels).
    public static func anchor(rect: CGRect, windows: [(id: UInt32, pid: Int32, frame: CGRect)]) -> ShapeAnchor? {
        let centre = CGPoint(x: rect.midX, y: rect.midY)
        // A window further back would read as covered at once, so only the front one may own it.
        guard let window = windows.first(where: { $0.frame.contains(centre) }),
              visibleShare(of: rect, in: [window.frame]) >= minVisible
        else { return nil }
        return ShapeAnchor(windowID: window.id, pid: window.pid, windowFrame: window.frame, rect: rect)
    }

    /// Share of `rect`'s area inside `frames` (assumed not to overlap, like displays).
    /// An empty rect counts as fully inside when its centre is.
    static func visibleShare(of rect: CGRect, in frames: [CGRect]) -> CGFloat {
        let area = rect.width * rect.height
        guard area > 0 else {
            return frames.contains { $0.contains(CGPoint(x: rect.midX, y: rect.midY)) } ? 1 : 0
        }
        let inside = frames.reduce(CGFloat(0)) { sum, frame in
            let part = rect.intersection(frame)
            return part.isNull ? sum : sum + part.width * part.height
        }
        return min(inside / area, 1)
    }
}

// MARK: - Tracker

public protocol WindowSource {
    /// Current state of the given window ids; an id absent from the result is closed.
    func sample(ids: [UInt32]) -> [UInt32: WindowSample]
}

public protocol TickScheduler {
    func start(_ tick: @escaping () -> Void)
    func stop()
    func setRate(hz: Double)
}

/// Result of a tick that changed something. Covers every anchored key the tracker knows:
/// a key is in exactly one of `rects` (visible there), `hidden` or `removed` (window gone 2 s).
public struct FollowUpdate: Equatable, Sendable {
    public var rects: [Int: CGRect] = [:]
    public var hidden: Set<Int> = []
    public var removed: Set<Int> = []

    public init(rects: [Int: CGRect] = [:], hidden: Set<Int> = [], removed: Set<Int> = []) {
        self.rects = rects
        self.hidden = hidden
        self.removed = removed
    }
}

/// Polls the anchored windows while any anchor exists; no timer at all otherwise.
/// Not thread-safe: call it only from the scheduler's serial queue.
public final class ShapeFollowTracker {
    public static let activeHz: Double = 30
    public static let idleHz: Double = 5
    /// No applied change for this long drops to idleHz; a window missing this long is removed.
    static let settleSeconds: TimeInterval = 2

    public var screens: [CGRect]
    public private(set) var isRunning = false

    private let source: WindowSource
    private let scheduler: TickScheduler
    private let now: () -> TimeInterval
    private let onUpdate: (FollowUpdate) -> Void
    private var anchors: [Int: ShapeAnchor] = [:]
    /// Last applied rect per key; absent while the shape is hidden.
    private var shown: [Int: CGRect] = [:]
    private var missingSince: [Int: TimeInterval] = [:]
    private var lastChange: TimeInterval = 0
    private var hz: Double = 0

    public init(source: WindowSource,
                scheduler: TickScheduler,
                screens: [CGRect],
                now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
                onUpdate: @escaping (FollowUpdate) -> Void = { _ in }) {
        self.source = source
        self.scheduler = scheduler
        self.screens = screens
        self.now = now
        self.onUpdate = onUpdate
    }

    /// Replaces the anchored set (key = the model's shape key). The first anchor starts the
    /// scheduler at activeHz; an empty set stops it.
    public func setAnchors(_ new: [Int: ShapeAnchor]) {
        for (key, anchor) in new where anchors[key] == nil { shown[key] = anchor.rect }
        anchors = new
        shown = shown.filter { new[$0.key] != nil }
        missingSince = missingSince.filter { new[$0.key] != nil }
        if new.isEmpty {
            halt()
        } else if !isRunning {
            isRunning = true
            scheduler.start { [weak self] in
                guard let self, let update = self.tick() else { return }
                self.onUpdate(update)
            }
            lastChange = now()
            setRate(Self.activeHz)
        }
    }

    /// One poll. nil when nothing visible changed (nothing to publish).
    public func tick() -> FollowUpdate? {
        guard !anchors.isEmpty else { return nil }
        let time = now()
        let samples = source.sample(ids: Array(Set(anchors.values.map(\.windowID))))
        var update = FollowUpdate()
        var changed = false
        for (key, anchor) in anchors {
            let sample = samples[anchor.windowID]
            if sample == nil || sample?.pid != anchor.pid {
                let since = missingSince[key] ?? time
                missingSince[key] = since
                if time - since >= Self.settleSeconds {
                    update.removed.insert(key)
                    changed = true
                    continue
                }
            } else {
                missingSince[key] = nil
            }
            if let rect = ShapeFollow.place(anchor, window: sample, screens: screens) {
                update.rects[key] = rect
                if shown[key].map({ abs($0.minX - rect.minX) >= 0.5 || abs($0.minY - rect.minY) >= 0.5 }) ?? true {
                    shown[key] = rect
                    changed = true
                }
            } else {
                update.hidden.insert(key)
                if shown.removeValue(forKey: key) != nil { changed = true }
            }
        }
        for key in update.removed {
            anchors[key] = nil
            shown[key] = nil
            missingSince[key] = nil
        }
        if changed {
            lastChange = time
            setRate(Self.activeHz)
        } else if time - lastChange >= Self.settleSeconds {
            setRate(Self.idleHz)
        }
        if anchors.isEmpty { halt() }  // every window closed: the removal path stops polling
        return changed ? update : nil
    }

    private func halt() {
        guard isRunning else { return }
        scheduler.stop()
        isRunning = false
        hz = 0
    }

    private func setRate(_ value: Double) {
        guard hz != value else { return }
        hz = value
        scheduler.setRate(hz: value)
    }
}
