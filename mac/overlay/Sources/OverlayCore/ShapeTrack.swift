import CoreGraphics
import Foundation

// Keeps highlight boxes on the thing they name: add-time re-resolve, element anchors (AX frame
// polling), editor line bands re-found after a scroll, and the rules that hide a window-anchored
// box when it can no longer be trusted. Pure: the app supplies AX/CG/OCR results
// (JarvisOverlay/ElementTrack.swift). Rects are CGRect in global top-left points.

/// How a box sits on its AX element.
public enum ElementFit: Equatable, Sendable {
    /// Text field holding the label: the box is the element frame + pad (survives text edits).
    case snap
    /// The box keeps its offset from the element's top-left (hidden when the element text changes).
    case offset
}

/// Tier A: a box tied to one AX element of its own window.
public struct ElementAnchor: Equatable, Sendable {
    public var fit: ElementFit
    /// Owning window at creation; `rect` is the box at creation.
    public var window: ShapeAnchor
    /// Registry handle of the AX element in the app (the element itself never leaves its queue).
    public var handle: Int
    public var elementFrame: CGRect
    /// ShapeTrack.elementText of the element at creation.
    public var text: String

    public init(fit: ElementFit, window: ShapeAnchor, handle: Int, elementFrame: CGRect, text: String) {
        self.fit = fit
        self.window = window
        self.handle = handle
        self.elementFrame = elementFrame
        self.text = text
    }
}

/// Tier B: an editor/terminal line band, re-found from the gutter after each scroll.
/// `window.rect` / `window.windowFrame` are from the latest gutter fit; `window.scrollBar` is the
/// scroll signal (nil = scroll wheel + content check only).
public struct BandAnchor: Equatable, Sendable {
    public var window: ShapeAnchor
    public var lines: ClosedRange<Int>

    public init(window: ShapeAnchor, lines: ClosedRange<Int>) {
        self.window = window
        self.lines = lines
    }
}

/// The anchor a tracked box got at add time (best tier available).
public enum TrackAnchor: Equatable, Sendable {
    case window(ShapeAnchor)
    case element(ElementAnchor)
    case band(BandAnchor)

    public var window: ShapeAnchor {
        switch self {
        case .window(let anchor): return anchor
        case .element(let anchor): return anchor.window
        case .band(let anchor): return anchor.window
        }
    }
}

/// One AX read of an element (or a scroll bar), stamped with its window frame at read time.
public struct ElementRead: Equatable, Sendable {
    public enum Status: Equatable, Sendable {
        case ok
        /// kAXErrorInvalidUIElement: the element is gone.
        case invalid
        /// Timeout / cannotComplete / no frame: the app may be hung.
        case timeout
    }

    public var status: Status
    public var frame: CGRect
    public var text: String
    /// Frame of the enclosing scroll/web area; nil = the window frame clips.
    public var clip: CGRect?
    public var windowFrame: CGRect
    /// Scroll bar reads only.
    public var scrollValue: Double?

    public init(status: Status, frame: CGRect = .zero, text: String = "", clip: CGRect? = nil,
                windowFrame: CGRect = .zero, scrollValue: Double? = nil) {
        self.status = status
        self.frame = frame
        self.text = text
        self.clip = clip
        self.windowFrame = windowFrame
        self.scrollValue = scrollValue
    }
}

public enum ShapeTrack {
    public typealias Window = (id: UInt32, pid: Int32, frame: CGRect)

    /// Same breathing room ground.py puts around a chosen box (SHAPE_PAD).
    public static let pad: CGFloat = 4
    static let hideBelow: CGFloat = 0.45
    static let showFrom: CGFloat = 0.55
    /// No AXTextArea: its value is a whole document, read at add time just to test a snap.
    public static let textRoles: Set<String> = ["AXTextField", "AXComboBox", "AXSearchField"]
    /// A box covering this share of its window is the window (or a page-sized container), not a
    /// thing inside it.
    static let windowShare: CGFloat = 0.6
    /// Labels stay below this line: the menu bar, with room for a notch.
    static let menuBarClearance: CGFloat = 48

    // MARK: hysteresis

    /// Visible-share limit for a box: a shown box hides below 0.45, a hidden one shows from 0.55.
    public static func threshold(wasShown: Bool) -> CGFloat { wasShown ? hideBelow : showFrom }

    // MARK: content check (boxes with no scroll signal)

    public static let gridSide = 16
    /// Mean-removed mean absolute difference, relative to the grids' own spread (mean absolute
    /// deviation), above which the content under a box changed. Relative, so thin or dim text
    /// counts like bold text: one text line replaced by another measures 0.27-0.45, a cleared
    /// line or a half-line scroll ~1.0, a one-character edit or a thin caret < 0.1, a uniform
    /// hover tint 0 (the mean is removed). A blinking block cursor measures ~0.5 and hides the
    /// box: hiding is the safe side.
    public static let contentThreshold: Double = 0.2
    /// Spread floor (0-255 luma) so a near-blank box does not divide by ~0.
    static let minSpread: Double = 4

    /// 16x16 mean luma of a row-major 8-bit gray buffer; [] when smaller than the grid.
    public static func lumaGrid(luma: [UInt8], width: Int, height: Int) -> [UInt8] {
        guard width >= gridSide, height >= gridSide, luma.count >= width * height else { return [] }
        var grid: [UInt8] = []
        grid.reserveCapacity(gridSide * gridSide)
        for gy in 0..<gridSide {
            let rows = (gy * height / gridSide)..<((gy + 1) * height / gridSide)
            for gx in 0..<gridSide {
                let columns = (gx * width / gridSide)..<((gx + 1) * width / gridSide)
                var sum = 0
                for y in rows { for x in columns { sum += Int(luma[y * width + x]) } }
                grid.append(UInt8(sum / (rows.count * columns.count)))
            }
        }
        return grid
    }

    /// Whether the content under a box changed since its reference grid (uniform tint ignored).
    public static func contentMoved(reference: [UInt8], current: [UInt8]) -> Bool {
        guard !reference.isEmpty, reference.count == current.count else { return false }
        let count = Double(reference.count)
        let meanA = reference.reduce(0) { $0 + Double($1) } / count
        let meanB = current.reduce(0) { $0 + Double($1) } / count
        let mad = zip(reference, current).reduce(0) { sum, pair in
            sum + abs((Double(pair.0) - meanA) - (Double(pair.1) - meanB))
        } / count
        let spreadA = reference.reduce(0) { $0 + abs(Double($1) - meanA) } / count
        let spreadB = current.reduce(0) { $0 + abs(Double($1) - meanB) } / count
        return mad / max(spreadA, spreadB, minSpread) > contentThreshold
    }

    // MARK: add-time re-resolve

    /// The single re-resolve attempt for a new shape: AX pick -> recheck, then the position
    /// lookup on a miss; OCR/model pick -> one refind. nil = nothing re-found.
    public static func reresolve<T>(hasCheck: Bool,
                                    recheck: () async -> T?,
                                    lookup: () async -> T?,
                                    refind: () async -> T?) async -> T? {
        guard hasCheck else { return await refind() }
        if let hit = await recheck() { return hit }
        return await lookup()
    }

    /// The fresh OCR observation for a shape label nearest the stale centre, padded; nil = no match.
    public static func refind(label: String, stale: CGRect, observations: [(text: String, rect: CGRect)]) -> CGRect? {
        let centre = CGPoint(x: stale.midX, y: stale.midY)
        func distance(_ rect: CGRect) -> CGFloat { hypot(rect.midX - centre.x, rect.midY - centre.y) }
        return observations.filter { textMatches(label, $0.text) }
            .min { distance($0.rect) < distance($1.rect) }?
            .rect.insetBy(dx: -pad, dy: -pad)
    }

    /// refind for a new shape. A grounded shape only looks in the window that holds its original
    /// rect (the same label in another window is a different thing); a model-written rect says
    /// nothing about its window, so any window may hold the label.
    public static func refind(label: String, stale: CGRect, observations: [(text: String, rect: CGRect)],
                              windows: [Window], grounded: Bool) -> CGRect? {
        refind(label: label, stale: stale,
               observations: grounded ? observations.filter { sameWindow($0.rect, as: stale, windows: windows) } : observations)
    }

    /// The frontmost window under the rect's centre (`windows` front to back); nil = none.
    public static func owner(of rect: CGRect, windows: [Window]) -> UInt32? {
        let centre = CGPoint(x: rect.midX, y: rect.midY)
        return windows.first { $0.frame.contains(centre) }?.id
    }

    /// Both rects lie in the same window (or both in none, e.g. the menu bar).
    public static func sameWindow(_ rect: CGRect, as stale: CGRect, windows: [Window]) -> Bool {
        owner(of: rect, windows: windows) == owner(of: stale, windows: windows)
    }

    /// Whether a shape's rect was a real screen item when the screen was captured: an OCR line or a
    /// window of the capture, padded like ground.py does. The brain's own shapes carry coordinates
    /// it wrote without seeing any box, so they match nothing here.
    public static func grounded(_ stale: CGRect, in captured: [CGRect]) -> Bool {
        captured.contains { item in
            let padded = item.insetBy(dx: -pad, dy: -pad)
            return abs(padded.minX - stale.minX) <= 1 && abs(padded.minY - stale.minY) <= 1
                && abs(padded.maxX - stale.maxX) <= 1 && abs(padded.maxY - stale.maxY) <= 1
        }
    }

    /// Tolerant label match (Vision reads identical pixels differently; the service truncates).
    public static func textMatches(_ label: String, _ text: String) -> Bool {
        let a = normalize(label), b = normalize(text)
        guard !a.isEmpty, !b.isEmpty else { return false }
        if a == b { return true }
        guard a.count >= 4, b.count >= 4 else { return false }
        if a.hasPrefix(b) || b.hasPrefix(a) { return true }
        let limit = max(1, a.count / 8)
        return abs(a.count - b.count) <= limit && editDistance(Array(a), Array(b)) <= limit
    }

    static func normalize(_ text: String) -> String {
        text.lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    static func editDistance(_ a: [Character], _ b: [Character]) -> Int {
        var previous = Array(0...b.count)
        for (i, ca) in a.enumerated() {
            var current = [i + 1]
            for (j, cb) in b.enumerated() {
                current.append(min(previous[j + 1] + 1, current[j] + 1, previous[j] + (ca == cb ? 0 : 1)))
            }
            previous = current
        }
        return previous[b.count]
    }

    /// Text compared by the offset-anchor rule (one composition for creation and polling).
    public static func elementText(title: String, description: String, value: String) -> String {
        [title, description, value].joined(separator: "\n")
    }

    /// Whether an AX element may carry the box, and the box it then gets. `value`/`label` are the
    /// element's own (label = title or description); `shapeLabel` is the shape's label.
    /// `mustName`: the element was only found by position at a stale spot, so an offset anchor
    /// also needs the element's own text to contain the shape label (else it may be a new thing).
    public static func acceptElement(box: CGRect, elementFrame: CGRect, role: String,
                                     value: String, label: String, shapeLabel: String,
                                     mustName: Bool = false) -> (fit: ElementFit, rect: CGRect)? {
        let wanted = normalize(shapeLabel)
        let named = !wanted.isEmpty && (normalize(value).contains(wanted) || normalize(label).contains(wanted))
        if textRoles.contains(role), named, fitsSnap(elementFrame, box: box) {
            return (.snap, elementFrame.insetBy(dx: -pad, dy: -pad))
        }
        if named || !mustName, elementFrame.contains(CGPoint(x: box.midX, y: box.midY)),
           elementFrame.width * elementFrame.height <= 4 * box.width * box.height {
            return (.offset, box)
        }
        return nil
    }

    /// A field the box may snap to is about one line tall: a tall multi-line field holding the word
    /// is not the word.
    static func fitsSnap(_ frame: CGRect, box: CGRect) -> Bool { frame.height <= 2.5 * box.height }

    /// The ancestor that shows a snapped text field, as an index into `ancestors` (nearest first);
    /// nil = the field itself. Safari keeps the URL's text field inside a wider wrapper that draws
    /// the focus ring. Taken: the outermost ancestor that contains the field (or the last one
    /// taken), is at most 1.6x as wide as the field and still fits a snap. Geometry only, as the
    /// wrapper's role differs between apps and versions.
    public static func fieldWrapper(box: CGRect, field: CGRect, ancestors: [CGRect]) -> Int? {
        var best: Int?
        var inner = field
        for (index, frame) in ancestors.enumerated()
        where frame.insetBy(dx: -1, dy: -1).contains(inner) && frame.width <= 1.6 * field.width && fitsSnap(frame, box: box) {
            best = index
            inner = frame
        }
        return best
    }

    /// Whether a box is its window (or a container nearly as big) rather than a thing inside it.
    public static func windowSized(_ rect: CGRect, window: CGRect) -> Bool {
        let part = rect.intersection(window)
        return !part.isNull && part.width * part.height >= windowShare * window.width * window.height
    }

    /// Centre y of a shape's label: above the box, never in the menu bar. A box near the top
    /// gets it just inside its top edge only when container-sized (>= 120 pt tall), else just below
    /// it, so it never covers a line of text such as Safari's address field (y33-94).
    public static func labelCentreY(for rect: CGRect) -> CGFloat {
        if rect.minY - 14 >= menuBarClearance { return rect.minY - 14 }
        return max(menuBarClearance, rect.height >= 120 ? rect.minY + 14 : rect.maxY + 14)
    }

    /// The element's own window in the CG list (pid + frame within 2 pt); nil -> hit-test fallback.
    public static func windowFor(elementWindowFrame frame: CGRect, pid: Int32, windows: [Window]) -> Window? {
        windows.first {
            $0.pid == pid && abs($0.frame.minX - frame.minX) <= 2 && abs($0.frame.minY - frame.minY) <= 2
                && abs($0.frame.width - frame.width) <= 2 && abs($0.frame.height - frame.height) <= 2
        }
    }

    /// A whole-window shape ("App: title", window frame + pad) re-found in the fresh window list:
    /// the frontmost window overlapping it by IoU >= 0.5, else the nearest window of its size.
    public static func matchWindow(stale: CGRect, windows: [Window]) -> CGRect? {
        // Payload windows are >= 80x60 (ScreenContext.visibleWindows), so smaller shapes are not windows.
        guard stale.width >= 80 + 2 * pad, stale.height >= 60 + 2 * pad else { return nil }
        let padded = windows.map { $0.frame.insetBy(dx: -pad, dy: -pad) }
        if let hit = padded.first(where: { iou($0, stale) >= 0.5 }) { return hit }
        func distance(_ rect: CGRect) -> CGFloat { hypot(rect.midX - stale.midX, rect.midY - stale.midY) }
        return padded.filter { abs($0.width - stale.width) <= 2 && abs($0.height - stale.height) <= 2 }
            .min { distance($0) < distance($1) }
    }

    static func iou(_ a: CGRect, _ b: CGRect) -> CGFloat {
        let part = a.intersection(b)
        guard !part.isNull else { return 0 }
        let inter = part.width * part.height
        let union = a.width * a.height + b.width * b.height - inter
        return union > 0 ? inter / union : 0
    }

    /// Where a shape without an element anchor goes: the re-found rect, else the original one,
    /// window-anchored by hit-test (tier C). `.gone` when its window holds it but it is mostly
    /// off-screen (it would be hidden at once), or when nothing was re-found for a rect that was
    /// never grounded (a model-written rect is drawn only where its label is seen), or when it
    /// covers most of its window (an ask for a thing in a window never draws the window; window
    /// picks take ElementTrack.windowShape instead); no window -> static, as before.
    public enum Placement: Equatable, Sendable {
        case anchored(ShapeAnchor)
        case unanchored(CGRect)
        case gone
    }

    public static func placement(fresh: CGRect?, stale: CGRect, windows: [Window], screens: [CGRect],
                                 grounded: Bool = true, windowPick: Bool = false) -> Placement {
        guard fresh != nil || grounded else { return .gone }
        let rect = fresh ?? stale
        guard let anchor = ShapeFollow.anchor(rect: rect, windows: windows) else { return .unanchored(rect) }
        return onScreen(rect, screens: screens) && (windowPick || !windowSized(rect, window: anchor.windowFrame)) ? .anchored(anchor) : .gone
    }

    public static func onScreen(_ rect: CGRect, screens: [CGRect]) -> Bool {
        ShapeFollow.visibleShare(of: rect, in: screens) >= ShapeFollow.minVisible
    }

    // MARK: tier A

    /// Box for an element anchor now, or nil (hidden). The element/clip rects come from a read
    /// stamped with `elementReadWindowFrame`; only window motion since that read is added.
    public static func placeElement(anchor: ElementAnchor, element: ElementRead, elementReadWindowFrame: CGRect,
                                    clip: CGRect?, window: WindowSample?, screens: [CGRect], wasShown: Bool) -> CGRect? {
        guard element.status == .ok, let window, window.pid == anchor.window.pid, window.isOnscreen else { return nil }
        // Recycled cells/rows change text; a snapped text field changes text when typed into.
        if anchor.fit == .offset, element.text != anchor.text { return nil }
        let dx = window.frame.minX - elementReadWindowFrame.minX
        let dy = window.frame.minY - elementReadWindowFrame.minY
        let base = anchor.fit == .snap
            ? element.frame.insetBy(dx: -pad, dy: -pad)
            : anchor.window.rect.offsetBy(dx: element.frame.minX - anchor.elementFrame.minX,
                                          dy: element.frame.minY - anchor.elementFrame.minY)
        let rect = base.offsetBy(dx: dx, dy: dy)
        let limit = threshold(wasShown: wasShown)
        let clipRect = clip?.offsetBy(dx: dx, dy: dy) ?? window.frame
        guard ShapeFollow.visibleShare(of: rect, in: [clipRect]) >= limit,
              ShapeFollow.shows(rect, in: window, screens: screens, threshold: limit)
        else { return nil }
        return rect
    }

    // MARK: tier B

    /// A 1-5 digit OCR string as a line number.
    public static func lineNumber(_ text: String) -> Int? {
        guard (1...5).contains(text.count), text.allSatisfy(\.isNumber) else { return nil }
        return Int(text)
    }

    /// Leftmost column of >= 5 line-number observations (1-5 digits, centre inside `window`,
    /// x-centres within 12 pt), as fit marks (its evenly spaced run, `LineBandMapper.evenRun`) plus
    /// the column's right edge. nil = no gutter.
    public static func gutterMarks(digits: [(line: Int, centre: CGPoint, right: CGFloat)],
                                   window: CGRect) -> (marks: [(line: Int, midY: CGFloat)], right: CGFloat)? {
        let sorted = digits.filter { window.contains($0.centre) }.sorted { $0.centre.x < $1.centre.x }
        var start = 0
        while start < sorted.count {
            var end = start
            while end + 1 < sorted.count, sorted[end + 1].centre.x - sorted[start].centre.x <= 12 {
                end += 1
            }
            if end - start + 1 >= 5 {
                let column = sorted[start...end]
                // Only the evenly spaced run: sticky-scroll header numbers are not on the line grid.
                return (LineBandMapper.evenRun(column.map { (line: $0.line, midY: $0.centre.y) }),
                        column.map(\.right).max() ?? 0)
            }
            start = end + 1
        }
        return nil
    }

    /// Band for `lines` from a fresh gutter fit; nil when the fit fails, the lines are outside the
    /// observed line numbers (never extrapolate) or the band is mostly outside the clip.
    public static func placeBand(lines: ClosedRange<Int>, marks: [(line: Int, midY: CGFloat)],
                                 x: CGFloat, width: CGFloat, clip: CGRect) -> CGRect? {
        guard let mapper = LineBandMapper(marks: marks),
              let low = marks.map(\.line).min(), let high = marks.map(\.line).max(),
              lines.lowerBound >= low, lines.upperBound <= high
        else { return nil }
        let band = mapper.band(for: lines, x: x, width: width)
        return ShapeFollow.visibleShare(of: band, in: [clip]) >= showFrom ? band : nil
    }

    /// A polled scroll-bar value that moved at all is a scroll.
    public static func scrolled(previous: Double?, current: Double?) -> Bool {
        guard let previous, let current else { return false }
        return previous != current
    }

    // MARK: tier C

    /// A scroll-wheel event at `point` goes to this window (inside it and not covered there).
    public static func scrollHits(_ point: CGPoint, window: WindowSample) -> Bool {
        window.isOnscreen && window.frame.contains(point) && !window.above.contains { $0.contains(point) }
    }

    /// Resized by more than 2 pt: in-window layout probably changed.
    public static func resized(from old: CGRect, to new: CGRect) -> Bool {
        abs(new.width - old.width) > 2 || abs(new.height - old.height) > 2
    }

    // MARK: idle clear

    /// Events that restart the overlay's 60 s idle clear: content arriving from the service.
    /// Follow ticks, content checks, late re-resolves and commits are not events and never do.
    public static func extendsIdle(_ event: OverlayEvent) -> Bool {
        switch event {
        case .say, .audio, .shape, .card, .lines: return true
        case .clear, .layout, .done, .unknown: return false
        }
    }

    // MARK: ordering

    /// Commit order for async adds: arrival order, and a clear beats every add begun before it.
    public struct AddOrder: Sendable {
        public struct Ticket: Equatable, Sendable {
            let serial: Int
            let generation: Int
        }

        private var generation = 0
        private var issued = 0
        private var committed = 0

        public init() {}

        public mutating func begin() -> Ticket {
            issued += 1
            return Ticket(serial: issued, generation: generation)
        }

        /// Every ticket begun so far can no longer commit.
        public mutating func clear() {
            generation += 1
            committed = issued
        }

        /// No clear since the ticket began (a late result may still be applied).
        public func survives(_ ticket: Ticket) -> Bool { ticket.generation == generation }

        public func canCommit(_ ticket: Ticket) -> Bool {
            ticket.generation == generation && ticket.serial == committed + 1
        }

        /// Marks the ticket done (also when it draws nothing); false = stale or out of order.
        public mutating func commit(_ ticket: Ticket) -> Bool {
            guard canCommit(ticket) else { return false }
            committed = ticket.serial
            return true
        }
    }
}
