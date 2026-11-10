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
    /// Registry handle of the vertical scroll bar under the box (tiers B and C); a value change
    /// is a scroll. nil = no AX scroll bar there.
    public var scrollBar: Int?

    public init(windowID: UInt32, pid: Int32, windowFrame: CGRect, rect: CGRect, scrollBar: Int? = nil) {
        self.windowID = windowID
        self.pid = pid
        self.windowFrame = windowFrame
        self.rect = rect
        self.scrollBar = scrollBar
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
    /// `threshold`: share that must stay visible (the tracker passes ShapeTrack.threshold(wasShown:)).
    public static func place(_ anchor: ShapeAnchor, window: WindowSample?, screens: [CGRect],
                             threshold: CGFloat = 0.5) -> CGRect? {
        guard let window, window.pid == anchor.pid, window.isOnscreen else { return nil }
        let rect = anchor.rect.offsetBy(dx: window.frame.minX - anchor.windowFrame.minX,
                                        dy: window.frame.minY - anchor.windowFrame.minY)
        return shows(rect, in: window, screens: screens, threshold: threshold) ? rect : nil
    }

    /// Enough of `rect` in its window and on a screen, and its centre not covered.
    static func shows(_ rect: CGRect, in window: WindowSample, screens: [CGRect], threshold: CGFloat) -> Bool {
        let centre = CGPoint(x: rect.midX, y: rect.midY)
        return visibleShare(of: rect, in: [window.frame]) >= threshold
            && visibleShare(of: rect, in: screens) >= threshold
            && !window.above.contains(where: { $0.contains(centre) })
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
/// a key is in exactly one of `rects` (visible there), `hidden` or `removed` (window gone 2 s,
/// element invalid 2 s, or a window box that can never be trusted again).
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

/// AX side of the tracker (JarvisOverlay/ElementTrack.swift). Every call returns at once: AX
/// IPC and captures run on the app's own queues and answer through `receive(pid:reads:)`,
/// `receiveBands(window:rects:windowFrame:)` and `receiveContent(_:rects:)` on the tracker's queue.
public protocol ElementSource {
    /// One read batch for one app: handle -> the window id its read is stamped with.
    func read(pid: Int32, handles: [Int: UInt32])
    /// One gutter re-find for one window: model key -> its line range.
    func refindBands(window: UInt32, lines: [Int: ClosedRange<Int>])
    /// One screen capture: the ShapeTrack.lumaGrid of each rect (model key -> rect).
    func checkContent(_ rects: [Int: CGRect])
    func setScrollMonitor(_ on: Bool)
    /// The tracker no longer uses these registry handles.
    func release(_ handles: Set<Int>)
}

/// Polls the anchored windows (and, through `elements`, AX elements) while any anchor exists;
/// no timer, monitor or element reference otherwise.
/// Not thread-safe: call it only from the scheduler's serial queue.
public final class ShapeFollowTracker {
    public static let activeHz: Double = 30
    public static let idleHz: Double = 5
    /// No applied change for this long drops to idleHz; a window missing this long is removed.
    static let settleSeconds: TimeInterval = 2
    /// Element boxes polled over AX (oldest first); later ones follow their window only.
    static let maxElementBoxes = 8
    /// An AX read every 3rd tick at activeHz (10 Hz); every tick at idleHz.
    static let readEvery = 3
    /// After an element moved inside its window, read on every tick for this long (smooth scroll).
    static let burstSeconds: TimeInterval = 0.4
    /// A hung app's element boxes keep their last place this long, then hide.
    static let hungSeconds: TimeInterval = 1
    /// Bands are re-found after this much scroll quiet, at most once per bandGap per window.
    static let bandQuiet: TimeInterval = 0.25
    static let bandGap: TimeInterval = 0.5
    /// A re-find or content check that never answered stops blocking the next one after this.
    static let refindStale: TimeInterval = 3
    /// Content check of boxes with no scroll signal: one capture per second, at most 4 boxes.
    static let contentEvery: TimeInterval = 1
    static let contentMax = 4

    public var screens: [CGRect]
    public private(set) var isRunning = false

    private let source: WindowSource
    private let scheduler: TickScheduler
    private let elements: ElementSource?
    private let now: () -> TimeInterval
    private let onUpdate: (FollowUpdate) -> Void
    /// The anchors as the model last sent them; `tracks` may hold newer band fits.
    private var given: [Int: TrackAnchor] = [:]
    private var tracks: [Int: TrackAnchor] = [:]
    /// Last applied rect per key; absent while the shape is hidden.
    private var shown: [Int: CGRect] = [:]
    private var missingSince: [Int: TimeInterval] = [:]
    private var lastChange: TimeInterval = 0
    private var hz: Double = 0
    private var ticks = 0
    private var samples: [UInt32: WindowSample] = [:]
    // Tier A: per registry handle, except invalidSince (per key).
    private var reads: [Int: ElementRead] = [:]
    private var failingSince: [Int: TimeInterval] = [:]
    private var invalidSince: [Int: TimeInterval] = [:]
    private var inFlight: [Int32: TimeInterval] = [:]
    private var scrollValues: [Int: Double] = [:]
    private var held: Set<Int> = []
    private var burstUntil: TimeInterval = -.infinity
    // Tiers B and C.
    private var retired: Set<Int> = []
    private var waiting: Set<Int> = []
    private var scrolledAt: [UInt32: TimeInterval] = [:]
    private var lastRefind: [UInt32: TimeInterval] = [:]
    private var refinding: [UInt32: TimeInterval] = [:]
    private var monitorOn = false
    // Content check, per key.
    private var references: [Int: [UInt8]] = [:]
    private var checkedAt: [Int: TimeInterval] = [:]
    private var lastContentCheck: TimeInterval = -.infinity
    private var contentInFlight: TimeInterval = -.infinity

    public init(source: WindowSource,
                scheduler: TickScheduler,
                screens: [CGRect],
                elements: ElementSource? = nil,
                now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
                onUpdate: @escaping (FollowUpdate) -> Void = { _ in }) {
        self.source = source
        self.scheduler = scheduler
        self.screens = screens
        self.elements = elements
        self.now = now
        self.onUpdate = onUpdate
    }

    /// Window anchors only (tier C).
    public func setAnchors(_ new: [Int: ShapeAnchor]) {
        setTracks(new.mapValues { .window($0) })
    }

    /// Replaces the tracked set (key = the model's shape key). The first anchor starts the
    /// scheduler at activeHz, a new or replaced anchor while running sets activeHz again; an
    /// empty set stops it.
    public func setTracks(_ new: [Int: TrackAnchor]) {
        // New keys, and keys whose anchor the model replaced (late re-resolve), start fresh;
        // the others keep the tracker's copy (a band's latest re-fit).
        let fresh = new.keys.filter { given[$0] != new[$0] }
        for key in fresh {
            forget(key)
            shown[key] = new[key]?.window.rect
        }
        given = new
        tracks = new.reduce(into: [:]) { out, item in out[item.key] = tracks[item.key] ?? item.value }
        shown = shown.filter { new[$0.key] != nil }
        missingSince = missingSince.filter { new[$0.key] != nil }
        invalidSince = invalidSince.filter { new[$0.key] != nil }
        references = references.filter { new[$0.key] != nil }
        checkedAt = checkedAt.filter { new[$0.key] != nil }
        retired = retired.filter { new[$0] != nil }
        waiting = waiting.filter { new[$0] != nil }
        pruneScrolls()
        syncHeld()
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
        } else if !fresh.isEmpty {
            wake(now())
        }
        syncMonitor()
    }

    /// One poll. nil when nothing visible changed (nothing to publish).
    public func tick() -> FollowUpdate? {
        guard !tracks.isEmpty else { return nil }
        let time = now()
        ticks += 1
        samples = source.sample(ids: Array(Set(tracks.values.map(\.window.windowID))))
        let polled = polledKeys()
        if hz == Self.idleHz || ticks % Self.readEvery == 0 || time < burstUntil {
            requestReads(polled, time: time)
        }
        requestRefinds(time: time)
        var update = FollowUpdate()
        var changed = false
        for (key, track) in tracks {
            let sample = samples[track.window.windowID]
            if sample == nil || sample?.pid != track.window.pid {
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
            let placed = place(key, track, sample: sample, polled: polled.contains(key), time: time)
            if placed.remove {
                update.removed.insert(key)
                changed = true
            } else if let rect = placed.rect {
                update.rects[key] = rect
                if shown[key].map({ abs($0.minX - rect.minX) >= 0.5 || abs($0.minY - rect.minY) >= 0.5
                    || abs($0.width - rect.width) >= 0.5 || abs($0.height - rect.height) >= 0.5 }) ?? true {
                    shown[key] = rect
                    changed = true
                }
            } else {
                update.hidden.insert(key)
                if shown.removeValue(forKey: key) != nil { changed = true }
            }
        }
        if !update.removed.isEmpty {
            for key in update.removed { forget(key) }
            pruneScrolls()
            syncHeld()
            syncMonitor()
        }
        requestContentChecks(time: time)
        if changed {
            lastChange = time
            setRate(Self.activeHz)
        } else if time - lastChange >= Self.settleSeconds {
            setRate(Self.idleHz)
        }
        if tracks.isEmpty { halt() }  // every window closed: the removal path stops polling
        return changed ? update : nil
    }

    /// Element reads of one app (call on the tracker's queue); used from the next tick on.
    public func receive(pid: Int32, reads new: [Int: ElementRead]) {
        inFlight[pid] = nil
        let time = now()
        for (handle, read) in new where held.contains(handle) {
            if let value = read.scrollValue {
                if ShapeTrack.scrolled(previous: scrollValues[handle], current: value) {
                    let windows = Set(tracks.values.filter { $0.window.scrollBar == handle }.map(\.window.windowID))
                    for window in windows { signalScroll(window: window, time: time) }
                    wake(time)
                }
                scrollValues[handle] = value
                continue
            }
            switch read.status {
            case .ok:
                if let last = reads[handle], last.status == .ok, Self.inWindow(last) != Self.inWindow(read) {
                    burstUntil = time + Self.burstSeconds  // moving inside its window: scroll or layout
                }
                reads[handle] = read
                failingSince[handle] = nil
            case .invalid:
                reads[handle] = read
                failingSince[handle] = nil
            case .timeout:
                failingSince[handle] = failingSince[handle] ?? time
            }
        }
    }

    /// Gutter re-find of one window (call on the tracker's queue): keys with a rect show there;
    /// keys without one stay hidden until the next scroll.
    public func receiveBands(window: UInt32, rects: [Int: CGRect], windowFrame: CGRect) {
        refinding[window] = nil
        guard scrolledAt[window] == nil else { return }  // scrolled again meanwhile: the next re-find decides
        for (key, track) in tracks {
            guard case .band(var band) = track, band.window.windowID == window, let rect = rects[key] else { continue }
            band.window.rect = rect
            band.window.windowFrame = windowFrame
            tracks[key] = .band(band)
            waiting.remove(key)
            references[key] = nil  // new spot: the next content check takes a new reference
            checkedAt[key] = nil
        }
    }

    /// Content grids from `checkContent` (call on the tracker's queue). The first grid of a box
    /// is its reference; a later one that differs removes a window box (it cannot follow) and makes a band
    /// re-find. A grid whose box moved while the capture ran is ignored.
    public func receiveContent(_ grids: [Int: [UInt8]], rects: [Int: CGRect]) {
        contentInFlight = -.infinity
        let time = now()
        for (key, grid) in grids where !grid.isEmpty {
            guard let track = tracks[key], let rect = rects[key], shown[key] == rect else { continue }
            guard let reference = references[key] else {
                references[key] = grid
                continue
            }
            guard ShapeTrack.contentMoved(reference: reference, current: grid) else { continue }
            if case .band = track {
                signalScroll(window: track.window.windowID, time: time)
            } else {
                retired.insert(key)
            }
            wake(time)
        }
    }

    /// A scroll-wheel event at a global top-left point (call on the tracker's queue).
    public func scrollWheel(at point: CGPoint) {
        let time = now()
        var hit = false
        for (id, sample) in samples where ShapeTrack.scrollHits(point, window: sample) {
            signalScroll(window: id, time: time)
            hit = true
        }
        if hit { wake(time) }  // the first scroll after idle must not wait at idleHz
    }

    private func place(_ key: Int, _ track: TrackAnchor, sample: WindowSample?, polled: Bool,
                       time: TimeInterval) -> (rect: CGRect?, remove: Bool) {
        let wasShown = shown[key] != nil
        let limit = ShapeTrack.threshold(wasShown: wasShown)
        switch track {
        case .element(let anchor) where polled:
            let read = reads[anchor.handle]
                ?? ElementRead(status: .ok, frame: anchor.elementFrame, text: anchor.text,
                               windowFrame: anchor.window.windowFrame)
            if read.status == .invalid {
                let since = invalidSince[key] ?? time
                invalidSince[key] = since
                return (nil, time - since >= Self.settleSeconds)
            }
            invalidSince[key] = nil
            if let stalled = failingSince[anchor.handle] ?? inFlight[anchor.window.pid],
               time - stalled >= Self.hungSeconds {
                return (nil, false)
            }
            return (ShapeTrack.placeElement(anchor: anchor, element: read, elementReadWindowFrame: read.windowFrame,
                                            clip: read.clip, window: sample, screens: screens,
                                            wasShown: wasShown), false)
        case .band(var band):
            if !waiting.contains(key), let sample, ShapeTrack.resized(from: band.window.windowFrame, to: sample.frame) {
                // Text may reflow: hide and re-find like a scroll; rebase so it fires once per resize.
                band.window.rect = band.window.rect.offsetBy(dx: sample.frame.minX - band.window.windowFrame.minX,
                                                             dy: sample.frame.minY - band.window.windowFrame.minY)
                band.window.windowFrame = sample.frame
                tracks[key] = .band(band)
                signalScroll(window: band.window.windowID, time: time)
            }
            if waiting.contains(key) { return (nil, false) }
            return (ShapeFollow.place(band.window, window: sample, screens: screens, threshold: limit), false)
        default:  // tier C: a window anchor, or an element box beyond the AX cap
            let anchor = track.window
            if let sample, ShapeTrack.resized(from: anchor.windowFrame, to: sample.frame) { retired.insert(key) }
            // Hidden for good: removed, so a box that can never come back costs no polling.
            if retired.contains(key) { return (nil, true) }
            return (ShapeFollow.place(anchor, window: sample, screens: screens, threshold: limit), false)
        }
    }

    /// Element frame relative to its window at read time.
    private static func inWindow(_ read: ElementRead) -> CGPoint {
        CGPoint(x: read.frame.minX - read.windowFrame.minX, y: read.frame.minY - read.windowFrame.minY)
    }

    /// A box whose rect covers its whole window ignores scroll (it cannot sit on a wrong element).
    private static func isWholeWindow(_ track: TrackAnchor) -> Bool {
        if case .window(let anchor) = track { return anchor.rect.contains(anchor.windowFrame) }
        return false
    }

    /// Oldest element boxes, at most maxElementBoxes (model keys grow with age).
    private func polledKeys() -> Set<Int> {
        Set(tracks.compactMap { item -> Int? in
            if case .element = item.value { return item.key }
            return nil
        }.sorted().prefix(Self.maxElementBoxes))
    }

    /// One batch per app, never a second one while its first is still out (hung app).
    private func requestReads(_ polled: Set<Int>, time: TimeInterval) {
        guard let elements else { return }
        var batches: [Int32: [Int: UInt32]] = [:]
        for (key, track) in tracks {
            let window = track.window
            if case .element(let anchor) = track, polled.contains(key) {
                batches[window.pid, default: [:]][anchor.handle] = window.windowID
            }
            if let bar = window.scrollBar { batches[window.pid, default: [:]][bar] = window.windowID }
        }
        for (pid, handles) in batches where inFlight[pid] == nil {
            inFlight[pid] = time
            elements.read(pid: pid, handles: handles)
        }
    }

    private func requestRefinds(time: TimeInterval) {
        guard let elements else { return }
        for (window, at) in scrolledAt where time - at >= Self.bandQuiet
            && time - (lastRefind[window] ?? -.infinity) >= Self.bandGap
            && time - (refinding[window] ?? -.infinity) >= Self.refindStale {
            scrolledAt[window] = nil
            var lines: [Int: ClosedRange<Int>] = [:]
            for (key, track) in tracks {
                if case .band(let band) = track, band.window.windowID == window { lines[key] = band.lines }
            }
            guard !lines.isEmpty else { continue }
            lastRefind[window] = time
            refinding[window] = time
            elements.refindBands(window: window, lines: lines)
        }
    }

    /// Up to contentMax shown boxes that AX does not follow, least recently checked first, once
    /// per contentEvery, one capture in flight. Boxes with a scroll bar are checked too: Terminal
    /// output moves the text while its scroll bar stays at the end. A box never checked yet is
    /// checked at the next tick, so its reference is taken where it was placed.
    private func requestContentChecks(time: TimeInterval) {
        guard let elements, time - contentInFlight >= Self.refindStale else { return }
        let polled = polledKeys()
        let candidates = tracks.compactMap { item -> Int? in
            let key = item.key, track = item.value
            guard shown[key] != nil, !retired.contains(key), !Self.isWholeWindow(track) else { return nil }
            switch track {
            case .window: return key
            case .element: return polled.contains(key) ? nil : key
            case .band: return waiting.contains(key) ? nil : key
            }
        }
        guard time - lastContentCheck >= Self.contentEvery || candidates.contains(where: { checkedAt[$0] == nil })
        else { return }
        let due = candidates.sorted { (checkedAt[$0] ?? -.infinity, $0) < (checkedAt[$1] ?? -.infinity, $1) }
            .prefix(Self.contentMax)
        guard !due.isEmpty else { return }
        lastContentCheck = time
        contentInFlight = time
        var rects: [Int: CGRect] = [:]
        for key in due {
            checkedAt[key] = time
            rects[key] = shown[key]
        }
        elements.checkContent(rects)
    }

    /// Bands of the window hide until re-found; window-anchored boxes there are removed
    /// (they cannot follow the content). Polled element boxes and whole-window boxes stay.
    private func signalScroll(window: UInt32, time: TimeInterval) {
        let polled = polledKeys()
        for (key, track) in tracks where track.window.windowID == window {
            switch track {
            case .band:
                waiting.insert(key)
                scrolledAt[window] = time
            case .element where polled.contains(key):
                break
            default:
                if !Self.isWholeWindow(track) { retired.insert(key) }
            }
        }
    }

    /// Back to activeHz at once (a scroll or a new box); the 2 s settle starts again.
    private func wake(_ time: TimeInterval) {
        guard isRunning else { return }
        lastChange = time
        setRate(Self.activeHz)
    }

    private func forget(_ key: Int) {
        tracks[key] = nil
        shown[key] = nil
        missingSince[key] = nil
        invalidSince[key] = nil
        references[key] = nil
        checkedAt[key] = nil
        retired.remove(key)
        waiting.remove(key)
    }

    /// Drops pending re-finds of windows that no longer hold a band.
    private func pruneScrolls() {
        let bandWindows = Set(tracks.values.compactMap { track -> UInt32? in
            if case .band(let band) = track { return band.window.windowID }
            return nil
        })
        scrolledAt = scrolledAt.filter { bandWindows.contains($0.key) }
    }

    /// Releases the registry handles no tracked box uses any more (a scroll bar shared by
    /// sibling bands stays while one of them is left).
    private func syncHeld() {
        var inUse = Set<Int>()
        for track in tracks.values {
            if case .element(let anchor) = track { inUse.insert(anchor.handle) }
            if let bar = track.window.scrollBar { inUse.insert(bar) }
        }
        let gone = held.subtracting(inUse)
        held = inUse
        guard !gone.isEmpty else { return }
        for handle in gone {
            reads[handle] = nil
            failingSince[handle] = nil
            scrollValues[handle] = nil
        }
        elements?.release(gone)
    }

    /// The scroll-wheel monitor runs only while a band or a window-anchored box that reacts to
    /// scroll exists (not for polled element boxes or whole-window boxes).
    private func syncMonitor() {
        let polled = polledKeys()
        let needed = tracks.contains { item in
            if case .element = item.value { return !polled.contains(item.key) }
            return !Self.isWholeWindow(item.value)
        }
        guard needed != monitorOn else { return }
        monitorOn = needed
        elements?.setScrollMonitor(needed)
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
