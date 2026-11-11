import Foundation
import CoreGraphics

/// Card placement over the whole visible screen: seeded, loose and layered (3x3 zones, least
/// occupied first), never on a keep-out (orb, highlight boxes), pairwise overlap 8-30 % of the
/// smaller card on one corner or a 20 pt gap. All rects are top-left global points, the space of `HUDShape.rect`.
/// Pure: no AppKit, so every rule here is unit-tested.
public enum ScatterLayout {
    /// Most cards on screen at once; the model drops the oldest beyond this.
    public static let maxCards = 18
    /// Gap kept to the edges of the visible frame (menu bar and Dock already excluded).
    public static let inset: CGFloat = 16
    /// Overlap of a pair, as a share of the smaller card.
    public static let maxOverlap: CGFloat = 0.30
    /// Summed overlaps of one card (an upper bound on how much of it is covered).
    public static let maxCoverage: CGFloat = 0.60
    /// Smallest overlap that reads as layered, not as a near miss.
    public static let minOverlap: CGFloat = 0.08
    /// Overlap a layered card looks for, as a share of the smaller card.
    public static let layerBand: ClosedRange<CGFloat> = 0.08...0.25
    /// Cards that do not overlap keep at least this gap; an overlap is at least this deep on each axis.
    public static let minGap: CGFloat = 20
    /// An overlap spans at most this share of the smaller card's width and of its height, so it
    /// sits on a corner and never runs along a whole side (a full-side strip reads as two cards
    /// butted together, or hides the card's title and value).
    public static let maxCornerShare: CGFloat = 0.75
    /// A press on a card that moves farther than this is a drag; less stays a click.
    public static let dragThreshold: CGFloat = 4
    static let candidatesPerZone = 24

    // Orb geometry, mirrors OverlayRootView.swift:26-30 (`.padding(24)`, bottom-right) and
    // OrbView.swift:53 (44x44). The banner band covers the status capsule above it.
    public static let orbSize: CGFloat = 44
    public static let orbPadding: CGFloat = 24
    public static let bannerBand: CGFloat = 40

    public static let chipSize = CGSize(width: 200, height: 28)
    public static let chipGap: CGFloat = 6
    /// The chip column starts this far below the top of the visible frame, clear of the
    /// window buttons of a window at the left edge.
    public static let chipTopClearance: CGFloat = 120

    public enum Placement: Equatable {
        case at(CGRect)
        /// Only overlap is in the way: retire the oldest card and call again.
        case retireOldest
        /// Every spot touches a keep-out, or (while aside, when nothing may retire) no spot keeps
        /// the caps: the card is not shown for now.
        case noRoom
    }

    /// Places one card. `existing` is oldest first. Zones fill least occupied first; a card
    /// either overlaps another by 8-30 % of the smaller one or keeps a 20 pt gap to it. From the
    /// 7th card on, about one in three (`prefersLayer`) looks for a spot overlapping exactly one
    /// older card by 8-25 % before anything else. The caps and the gap always hold; with
    /// `allowRetire` false (parked cards are never retired) a full screen gives `.noRoom`.
    public static func place(size: CGSize, key: String, visible: CGRect, existing: [CGRect],
                             keepOut: [CGRect], allowRetire: Bool = true) -> Placement {
        let area = insetArea(visible)
        guard area.width > 0, area.height > 0 else { return .noRoom }
        let size = CGSize(width: min(size.width, area.width), height: min(size.height, area.height))
        var rng = SplitMix64(state: seed(key))
        var occupied = [Int](repeating: 0, count: 9)
        for frame in existing { occupied[zone(of: CGPoint(x: frame.midX, y: frame.midY), in: area)] += 1 }
        let covered = coverage(existing)
        let tieBreak = (0..<9).map { _ in rng.next() }
        let zones = (0..<9).sorted { (occupied[$0], tieBreak[$0]) < (occupied[$1], tieBreak[$1]) }
        let zoneSize = CGSize(width: area.width / 3, height: area.height / 3)
        let layered = prefersLayer(key: key, existingCount: existing.count)
        var best: (frame: CGRect, score: CGFloat)?
        var clearOfKeepOut = false
        for zone in zones {
            let zoneOrigin = CGPoint(x: area.minX + CGFloat(zone % 3) * zoneSize.width,
                                     y: area.minY + CGFloat(zone / 3) * zoneSize.height)
            var origins: [CGPoint] = []
            for _ in 0..<candidatesPerZone {
                origins.append(CGPoint(x: zoneOrigin.x + rng.unit() * zoneSize.width - size.width / 2,
                                       y: zoneOrigin.y + rng.unit() * zoneSize.height - size.height / 2))
            }
            if layered {
                for other in existing {
                    for origin in layerOrigins(size: size, over: other)
                    where self.zone(of: CGPoint(x: origin.x + size.width / 2, y: origin.y + size.height / 2), in: area) == zone {
                        origins.insert(origin, at: 0)
                    }
                }
            }
            var layer: CGRect?
            for origin in origins {
                let frame = clamp(CGRect(origin: origin, size: size), into: area)
                if keepOut.contains(where: { $0.intersects(frame) }) { continue }
                clearOfKeepOut = true
                guard let fit = fitness(frame, existing: existing, covered: covered) else { continue }
                if layered, layer == nil, fit.layers { layer = frame }
                if fit.overlap < (best?.score ?? .infinity) { best = (frame, fit.overlap) }
            }
            if let layer { return .at(layer) }
            if !layered, let best, best.score == 0 { return .at(best.frame) }
        }
        if let best { return .at(best.frame) }
        return clearOfKeepOut && !existing.isEmpty && allowRetire ? .retireOldest : .noRoom
    }

    /// From the 7th card on, about one card in three, fixed by its key's seed.
    static func prefersLayer(key: String, existingCount: Int) -> Bool {
        existingCount >= 6 && (seed(key) >> 7) % 3 == 0
    }

    /// Four origins that put a card of `size` over one corner of `other`, overlapping about 15 %
    /// of the smaller of the two.
    static func layerOrigins(size: CGSize, over other: CGRect) -> [CGPoint] {
        let overlapWidth = min(size.width, other.width) * 0.4
        let overlapHeight = min(size.height, other.height,
                                0.15 * min(size.width * size.height, area(other)) / overlapWidth)
        var origins: [CGPoint] = []
        for x in [other.maxX - overlapWidth, other.minX - size.width + overlapWidth] {
            for y in [other.maxY - overlapHeight, other.minY - size.height + overlapHeight] {
                origins.append(CGPoint(x: x, y: y))
            }
        }
        return origins
    }

    /// Summed overlap with the existing cards, and whether it overlaps exactly one of them by
    /// `layerBand`. Nil when a pair is closer than `minGap` without overlapping, overlaps outside
    /// 8-30 % of the smaller card or off a corner, or any card would pass `maxCoverage`.
    /// `existing[newerFrom...]` lie on top of `frame`, so their corner rule runs the other way.
    static func fitness(_ frame: CGRect, existing: [CGRect], covered: [CGFloat],
                        newerFrom: Int = .max) -> (overlap: CGFloat, layers: Bool)? {
        var total: CGFloat = 0
        var overlapped = 0
        var inBand = true
        for (index, other) in existing.enumerated() {
            let overlap = overlapArea(frame, other)
            guard overlap > 0 else {
                if frame.intersects(other.insetBy(dx: -minGap, dy: -minGap)) { return nil }
                continue
            }
            let ratio = overlap / min(area(frame), area(other))
            if ratio < minOverlap || ratio > maxOverlap || (covered[index] + overlap) / area(other) > maxCoverage
                || !(index < newerFrom ? isCornerOverlap(frame, over: other) : isCornerOverlap(other, over: frame)) {
                return nil
            }
            overlapped += 1
            if !layerBand.contains(ratio) { inBand = false }
            total += overlap
        }
        if area(frame) > 0, total / area(frame) > maxCoverage { return nil }
        return (total, overlapped == 1 && inBand)
    }

    /// `top` lies over one corner of `below`: the shared part is `minGap` to `maxCornerShare` of
    /// the smaller card deep on each axis, and `below`'s top-left corner (where its title
    /// starts) stays uncovered.
    static func isCornerOverlap(_ top: CGRect, over below: CGRect) -> Bool {
        let shared = top.intersection(below)
        return shared.width >= minGap && shared.width <= maxCornerShare * min(top.width, below.width)
            && shared.height >= minGap && shared.height <= maxCornerShare * min(top.height, below.height)
            && (shared.minX > below.minX || shared.minY > below.minY)
    }

    /// How much of each card the others cover.
    static func coverage(_ frames: [CGRect]) -> [CGFloat] {
        frames.indices.map { index in
            frames.indices.reduce(CGFloat(0)) { $0 + ($1 == index ? 0 : overlapArea(frames[index], frames[$1])) }
        }
    }

    /// The orb, its padding and the banner band, bottom-right of the screen.
    public static func orbKeepOut(screen: CGRect) -> CGRect {
        let reach = orbPadding + orbSize + orbPadding
        return CGRect(x: screen.maxX - reach, y: screen.maxY - reach - bannerBand,
                      width: reach + orbPadding, height: reach + bannerBand + orbPadding)
    }

    /// A highlight box plus its label band (ShapeView.swift: centred at the clamped midX,
    /// `ShapeTrack.labelCentreY`, about 120x24), inflated by 8 pt.
    public static func shapeKeepOut(_ shape: CGRect, screenWidth: CGFloat) -> CGRect {
        let labelX = min(max(shape.midX, 60), screenWidth - 60)
        let label = CGRect(x: labelX - 60, y: ShapeTrack.labelCentreY(for: shape) - 12, width: 120, height: 24)
        return shape.union(label).insetBy(dx: -8, dy: -8)
    }

    /// Moves each frame the least distance needed to lie inside the new inset area.
    public static func reclamp(frames: [String: CGRect], visible: CGRect) -> [String: CGRect] {
        let area = insetArea(visible)
        return frames.mapValues { clamp($0, into: area) }
    }

    /// Parked chips in a column on the edge away from the orb, oldest on top; a full column
    /// continues in the next one.
    public static func chipFrames(keys: [String], visible: CGRect, orbOnRight: Bool) -> [String: CGRect] {
        let area = insetArea(visible)
        var frames: [String: CGRect] = [:]
        let top = max(area.minY, visible.minY + chipTopClearance)
        var column: CGFloat = 0
        var y = top
        for key in keys {
            if y + chipSize.height > area.maxY, y > top {
                column += 1
                y = top
            }
            let step = column * (chipSize.width + chipGap)
            let x = orbOnRight ? area.minX + step : area.maxX - chipSize.width - step
            frames[key] = CGRect(x: x, y: y, width: chipSize.width, height: chipSize.height)
            y += chipSize.height + chipGap
        }
        return frames
    }

    /// Smallest rect holding every frame; nil for none.
    static func bounds(_ frames: [CGRect]) -> CGRect? {
        frames.reduce(nil as CGRect?) { $0?.union($1) ?? $1 }
    }

    /// A group move shorter than this would not read as a move: squeeze instead.
    public static let visibleShift: CGFloat = 24
    /// Each squeezed card keeps at least this much of itself uncovered along the move axis (its
    /// title row, one touch target).
    public static let minStrip: CGFloat = 44

    /// New starts along one axis, anchored at `to` (atEnd) or `from`. A plain shift to the anchor
    /// edge comes first and keeps the layout, when the group fits `limit` (the inset span) and the
    /// shift is at least `visibleShift` (or the group already fits `from...to`). Otherwise the group
    /// is squeezed toward `from...to`, order kept, and every card stays at least `minStrip` past the
    /// cards before it that share its `across` span (the other axis: cards in another row or
    /// column cannot cover it), so it runs on past `to` (toward the far end of `limit`) when the
    /// strips need it.
    static func packed(_ starts: [CGFloat], lengths: [CGFloat], across: [ClosedRange<CGFloat>],
                       from: CGFloat, to: CGFloat, atEnd: Bool, limit: ClosedRange<CGFloat>) -> [CGFloat] {
        let ends = zip(starts, lengths).map { $0 + $1 }
        guard let low = starts.min(), let high = ends.max(), let longest = lengths.max() else { return starts }
        let span = high - low, room = to - from
        let shift = atEnd ? to - high : from - low
        if span <= limit.upperBound - limit.lowerBound, span <= room || abs(shift) >= visibleShift {
            return starts.map { $0 + shift }
        }
        let scale = span <= longest ? 1 : max(0, room - longest) / (span - longest)
        var out = zip(starts, ends).map { start, end in
            atEnd ? to - (high - end) * scale - (end - start) : from + (start - low) * scale
        }
        // Walk away from the anchor edge: each card starts at least minStrip past the previous one.
        let walk = out.indices.sorted {
            atEnd ? (out[$0], starts[$0]) > (out[$1], starts[$1]) : (out[$0], starts[$0]) < (out[$1], starts[$1])
        }
        for (step, index) in walk.enumerated() {
            for before in walk[..<step] where across.indices.contains(before) && across[before].overlaps(across[index]) {
                out[index] = atEnd ? min(out[index], out[before] - minStrip) : max(out[index], out[before] + minStrip)
            }
        }
        return out
    }

    /// `frames` moved off every keep-out (orb, permission card) by the shortest shared shift (up,
    /// down, left, right, up and left, down and left) that keeps the group inside `area`; `frames`
    /// itself when nothing is covered; nil when no shift works.
    static func clearOf(_ keepOut: [CGRect], _ frames: [CGRect], area: CGRect) -> [CGRect]? {
        func covers(_ set: [CGRect]) -> Bool {
            set.contains { frame in keepOut.contains { overlapArea(frame, $0) > 0 } }
        }
        guard covers(frames) else { return frames }
        let hits = frames.flatMap { frame in keepOut.filter { overlapArea(frame, $0) > 0 }.map { (frame, $0) } }
        let up = hits.map { $0.0.maxY - $0.1.minY }.max() ?? 0
        let down = hits.map { $0.1.maxY - $0.0.minY }.max() ?? 0
        let left = hits.map { $0.0.maxX - $0.1.minX }.max() ?? 0
        let right = hits.map { $0.1.maxX - $0.0.minX }.max() ?? 0
        let shifts = [CGVector(dx: 0, dy: -up), CGVector(dx: 0, dy: down), CGVector(dx: -left, dy: 0),
                      CGVector(dx: right, dy: 0), CGVector(dx: -left, dy: -up), CGVector(dx: -left, dy: down)]
            .sorted { hypot($0.dx, $0.dy) < hypot($1.dx, $1.dy) }
        let room = area.insetBy(dx: -0.5, dy: -0.5)
        for shift in shifts {
            let set = frames.map { $0.offsetBy(dx: shift.dx, dy: shift.dy) }
            if !covers(set), let box = bounds(set), room.contains(box) { return set }
        }
        return nil
    }

    /// Duration of a fade or a short overlay transition.
    public static let motionShort: TimeInterval = 0.2
    /// Longest voice-move slide: a move across the whole visible width.
    public static let moveMaxDuration: TimeInterval = 0.45
    /// Length of the refused-move nudge (out and back).
    public static let nudgeDuration: TimeInterval = 0.12
    /// How far the refused-move nudge leans toward the asked side.
    public static let nudgeDistance: CGFloat = 10

    /// A voice move slides longer the farther it goes, relative to the screen width (so an
    /// ultrawide move is not faster than a laptop one): `moveMaxDuration` for the full width.
    public static func moveDuration(distance: CGFloat, across width: CGFloat) -> TimeInterval {
        guard width > 0 else { return moveMaxDuration }
        return min(moveMaxDuration, max(motionShort, moveMaxDuration * TimeInterval(distance / width)))
    }

    /// Top-left-space lean of the refused-move nudge toward the asked side.
    public static func nudgeOffset(_ direction: LayoutMode) -> CGVector {
        let d = nudgeDistance
        switch direction {
        case .left: return CGVector(dx: -d, dy: 0)
        case .right, .otherSide: return CGVector(dx: d, dy: 0)
        case .up: return CGVector(dx: 0, dy: -d)
        case .down: return CGVector(dx: 0, dy: d)
        case .topLeft: return CGVector(dx: -d, dy: -d)
        case .topRight: return CGVector(dx: d, dy: -d)
        case .bottomLeft: return CGVector(dx: -d, dy: d)
        case .bottomRight: return CGVector(dx: d, dy: d)
        case .aside, .restore, .hide, .show: return CGVector(dx: 0, dy: 0)
        }
    }

    /// The agent's Confirm card (and its settled / expired replacement): shown in full on arrival,
    /// never as a parked chip. Same id as `_CONFIRM_CARD_ID` in gateway/overlay.py.
    public static let confirmCardID = "agentconfirm"

    public static func restoresParked(_ card: HUDCard) -> Bool {
        card.id == confirmCardID
    }

    /// FNV-1a 64 over the UTF-8 bytes; `hashValue` is randomised per process.
    public static func seed(_ key: String) -> UInt64 {
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in key.utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 0x0000_0100_0000_01b3
        }
        return hash
    }

    public static func insetArea(_ visible: CGRect) -> CGRect {
        visible.insetBy(dx: inset, dy: inset)
    }

    /// Shrinks `rect` to fit, then moves it the least distance into `area`.
    public static func clamp(_ rect: CGRect, into area: CGRect) -> CGRect {
        guard !area.isNull, !area.isEmpty else { return rect }
        let width = min(rect.width, area.width), height = min(rect.height, area.height)
        return CGRect(x: min(max(rect.minX, area.minX), area.maxX - width),
                      y: min(max(rect.minY, area.minY), area.maxY - height),
                      width: width, height: height)
    }

    static func zone(of point: CGPoint, in area: CGRect) -> Int {
        let col = min(2, max(0, Int((point.x - area.minX) / (area.width / 3))))
        let row = min(2, max(0, Int((point.y - area.minY) / (area.height / 3))))
        return row * 3 + col
    }

    static func overlapArea(_ a: CGRect, _ b: CGRect) -> CGFloat {
        let overlap = a.intersection(b)
        return overlap.isNull ? 0 : overlap.width * overlap.height
    }

    static func area(_ rect: CGRect) -> CGFloat { rect.width * rect.height }

    /// Idle deadline rule: wait another round only while the pointer rests inside a shown panel
    /// (decided from the pointer position now, never from remembered hover events).
    public static func pointerRests(_ pointer: CGPoint, onAnyOf frames: [CGRect]) -> Bool {
        frames.contains { $0.contains(pointer) }
    }

    /// A playing video holds the idle clear for at most this long from play start.
    public static let maxPlayHold: TimeInterval = 600

    /// Idle deadline: wait another round when the pointer rests on a panel, or a video started
    /// playing less than maxPlayHold ago. playingSince nil = nothing playing.
    public static func idleWaits(pointerOnPanel: Bool, playingSince: Date?, now: Date) -> Bool {
        if pointerOnPanel { return true }
        guard let playingSince else { return false }
        return now.timeIntervalSince(playingSince) < maxPlayHold
    }

    /// Whether a press that started at `start` and is now at `pointer` has become a drag.
    public static func isDrag(from start: CGPoint, to pointer: CGPoint) -> Bool {
        hypot(pointer.x - start.x, pointer.y - start.y) > dragThreshold
    }

    /// SplitMix64: small, seedable, the same sequence on every run and machine.
    struct SplitMix64 {
        var state: UInt64

        mutating func next() -> UInt64 {
            state &+= 0x9E37_79B9_7F4A_7C15
            var z = state
            z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
            z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
            return z ^ (z >> 31)
        }

        /// Uniform in [0, 1).
        mutating func unit() -> CGFloat {
            CGFloat(Double(next() >> 11) * 0x1.0p-53)
        }
    }

    // MARK: - Layout state

    /// Stored scatter frames plus the normal / aside mode. A frame never changes when another
    /// card comes or goes; only its own replacement, a drag, a screen change (reclamp) or
    /// removal does. `order` is also the z-order every panel pass stacks by.
    public struct LayoutState: Equatable {
        public enum Mode: Equatable { case normal, aside }

        public private(set) var mode: Mode = .normal
        /// Stored frame per card key; kept while aside, so restore puts each card back.
        public private(set) var frames: [String: CGRect] = [:]
        /// Placed keys, back to front: arrival order, a re-placed or dragged card moves to the end.
        public private(set) var order: [String] = []
        /// Cards the user dragged: their frame is the user's choice, never re-placed.
        public private(set) var pinned: Set<String> = []
        /// Cards with no spot yet (no room while aside, or only a keep-out in the way), oldest first.
        public private(set) var waiting: [String] = []
        /// Hidden: no card or chip shows; frames, order, pins and the mode are kept for show().
        public private(set) var hidden = false
        /// Bumped by every group move, so the panels animate that change (and no other).
        public private(set) var moveCount = 0

        public init() {}

        /// Keys shown as chips: every card while aside (placed or still waiting), none otherwise.
        public var chipKeys: [String] { mode == .aside && !hidden ? order + waiting : [] }
        /// Keys shown as cards at their frames: every placed card while normal and not hidden.
        public var visibleKeys: [String] { mode == .normal && !hidden ? order : [] }

        /// Who goes first when room is needed: unpinned cards, back to front, then pinned ones.
        public var retireOrder: [String] { order.filter { !pinned.contains($0) } + order.filter { pinned.contains($0) } }

        public mutating func aside() { mode = .aside }
        public mutating func restore() { mode = .normal }
        public mutating func clear() { self = LayoutState() }
        public mutating func hide() { hidden = true }
        public mutating func show() { hidden = false }

        /// Moves every placed card as one group toward the named side of the inset area. Only the
        /// named axes change. A plain shift to the named edge comes first and keeps the layout; when
        /// that would not show (under `visibleShift`) or not fit, the group is squeezed into the named
        /// half (edges) or quarter (corners), order kept, each card keeping a `minStrip`, running past
        /// the half only as far as the strips need. otherSide mirrors a group that lies on one side of
        /// the centre line and sends a straddling one to the half away from its centre. A group that
        /// would cover a keep-out (orb, permission card) takes the shortest shared shift that clears
        /// it and stays inside. Returns false, changing nothing (still hidden or parked), when no
        /// shift clears the keep-outs, there is nothing to move, or the mode is not a move.
        /// Otherwise shows, restores, moves and pins the cards.
        @discardableResult
        public mutating func moveAll(_ direction: LayoutMode, visible: CGRect, keepOut: [CGRect] = []) -> Bool {
            let area = ScatterLayout.insetArea(visible)
            let keys = order.filter { frames[$0] != nil }
            let old = keys.compactMap { frames[$0] }
            guard let group = ScatterLayout.bounds(old), area.width > 0, area.height > 0 else { return false }
            let halfX = area.width / 2, halfY = area.height / 2
            let spanX = area.minX...area.maxX, spanY = area.minY...area.maxY
            var xs = old.map(\.minX), ys = old.map(\.minY)
            let widths = old.map(\.width), heights = old.map(\.height)
            let rows: () -> [ClosedRange<CGFloat>] = { old.indices.map { ys[$0]...(ys[$0] + heights[$0]) } }
            let columns: () -> [ClosedRange<CGFloat>] = { old.indices.map { xs[$0]...(xs[$0] + widths[$0]) } }
            let toLeft = {
                xs = ScatterLayout.packed(xs, lengths: widths, across: rows(), from: area.minX, to: area.minX + halfX, atEnd: false, limit: spanX)
            }
            let toRight = {
                xs = ScatterLayout.packed(xs, lengths: widths, across: rows(), from: area.maxX - halfX, to: area.maxX, atEnd: true, limit: spanX)
            }
            let toTop = {
                ys = ScatterLayout.packed(ys, lengths: heights, across: columns(), from: area.minY, to: area.minY + halfY, atEnd: false, limit: spanY)
            }
            let toBottom = {
                ys = ScatterLayout.packed(ys, lengths: heights, across: columns(), from: area.maxY - halfY, to: area.maxY, atEnd: true, limit: spanY)
            }
            switch direction {
            case .left: toLeft()
            case .right: toRight()
            case .up: toTop()
            case .down: toBottom()
            case .topLeft: toLeft(); toTop()
            case .topRight: toRight(); toTop()
            case .bottomLeft: toLeft(); toBottom()
            case .bottomRight: toRight(); toBottom()
            case .otherSide:
                if group.maxX <= visible.midX || group.minX >= visible.midX {
                    let mirroredMin = 2 * visible.midX - group.maxX
                    xs = xs.map { mirroredMin + $0 - group.minX }
                } else if group.midX <= visible.midX {
                    toRight()
                } else {
                    toLeft()
                }
            case .aside, .restore, .hide, .show: return false
            }
            var moved = old.indices.map { CGRect(x: xs[$0], y: ys[$0], width: widths[$0], height: heights[$0]) }
            // One shared offset keeps the group inside the inset area on the axis it did not move.
            if let box = ScatterLayout.bounds(moved) {
                let inside = ScatterLayout.clamp(box, into: area)
                if inside.size == box.size {
                    moved = moved.map { $0.offsetBy(dx: inside.minX - box.minX, dy: inside.minY - box.minY) }
                }
            }
            moved = moved.map { ScatterLayout.clamp($0, into: area) }  // only a card past the area edge moves here
            guard let placed = ScatterLayout.clearOf(keepOut, moved, area: area) else { return false }
            hidden = false
            mode = .normal
            for (index, key) in keys.enumerated() {
                frames[key] = placed[index]
                pinned.insert(key)
            }
            moveCount &+= 1
            return true
        }

        /// Places a measured card against the stored frames. Returns the keys retired to make
        /// room, oldest first. The card stays unplaced when only a keep-out is in the way, or
        /// while aside when no spot keeps the caps (nothing is retired then); a later sync after
        /// restore places it.
        @discardableResult
        public mutating func cardAdded(_ key: String, size: CGSize, visible: CGRect, keepOut: [CGRect]) -> [String] {
            cardRemoved(key)
            var retired: [String] = []
            while true {
                let placement = ScatterLayout.place(size: size, key: key, visible: visible,
                                                    existing: order.compactMap { frames[$0] },
                                                    keepOut: keepOut, allowRetire: mode == .normal)
                switch placement {
                case .at(let frame):
                    frames[key] = frame
                    order.append(key)
                    return retired
                case .retireOldest:
                    guard let oldest = retireOrder.first else { return retired }
                    retired.append(oldest)
                    cardRemoved(oldest)
                case .noRoom:
                    waiting.append(key)
                    return retired
                }
            }
        }

        /// A card replaced in place keeps its frame when its size is unchanged, and keeps its
        /// origin while the new size still keeps the caps, the gap and the keep-outs against the
        /// unpinned cards (older ones below it, newer ones on top); otherwise that card alone is
        /// placed again (no other card moves), retiring the oldest if needed. Returns the retired
        /// keys. A pinned card only stays on screen. A highlight already under the old frame
        /// does not move it (the card dims instead).
        @discardableResult
        public mutating func cardResized(_ key: String, size: CGSize, visible: CGRect, keepOut: [CGRect]) -> [String] {
            guard let old = frames[key], let position = order.firstIndex(of: key) else { return [] }
            let moved = CGRect(origin: old.origin, size: size)
            if pinned.contains(key) {
                frames[key] = ScatterLayout.clamp(moved, into: visible)
                return []
            }
            let frame = ScatterLayout.clamp(moved, into: ScatterLayout.insetArea(visible))
            guard frame != old else { return [] }
            // Overlaps with a pinned card are the user's choice, not something the new size broke.
            let older = order[..<position].filter { !pinned.contains($0) }
            let newer = order[(position + 1)...].filter { !pinned.contains($0) }
            let others = (older + newer).compactMap { frames[$0] }
            if !keepOut.contains(where: { $0.intersects(frame) && !$0.intersects(old) }),
               ScatterLayout.fitness(frame, existing: others, covered: ScatterLayout.coverage(others),
                                     newerFrom: older.count) != nil {
                frames[key] = frame
                return []
            }
            return cardAdded(key, size: size, visible: visible, keepOut: keepOut)
        }

        /// New line highlights arrived after the cards (a `lines` band waits for the editor
        /// scroll): each unpinned card that covers one is placed again, once, against `keepOut`
        /// (which holds the new highlights). A card with no other spot stays where it is (it dims).
        /// Returns the keys retired to make room.
        @discardableResult
        public mutating func moveOff(_ highlights: [CGRect], visible: CGRect, keepOut: [CGRect]) -> [String] {
            guard mode == .normal else { return [] }  // aside: cards are chips, nothing covers the code
            var retired: [String] = []
            for key in order where !pinned.contains(key) && !retired.contains(key) {
                guard let frame = frames[key], highlights.contains(where: { $0.intersects(frame) }) else { continue }
                let before = self
                let gone = cardAdded(key, size: frame.size, visible: visible, keepOut: keepOut)
                if frames[key] == nil { self = before } else { retired += gone }
            }
            return retired
        }

        /// The user drags a card (called when the drag starts and when it is dropped): it comes
        /// to the front, is pinned, and its frame is clamped fully inside the visible frame.
        public mutating func cardDragged(_ key: String, to frame: CGRect, visible: CGRect) {
            guard frames[key] != nil else { return }
            frames[key] = ScatterLayout.clamp(frame, into: visible)
            pinned.insert(key)
            order.removeAll { $0 == key }
            order.append(key)
        }

        public mutating func cardRemoved(_ key: String) {
            frames[key] = nil
            order.removeAll { $0 == key }
            pinned.remove(key)
            waiting.removeAll { $0 == key }
        }

        /// Forgets every card not in `live` (cap, idle clear, clear).
        public mutating func keep(_ live: Set<String>) {
            order.removeAll { !live.contains($0) }
            waiting.removeAll { !live.contains($0) }
            frames = frames.filter { live.contains($0.key) }
            pinned = pinned.filter { live.contains($0) }
        }

        /// The screen changed: scattered cards move back into the inset area, a pinned card
        /// only as far as it takes to stay fully inside the visible frame.
        public mutating func reclamp(visible: CGRect) {
            let area = ScatterLayout.insetArea(visible)
            for (key, frame) in frames {
                frames[key] = ScatterLayout.clamp(frame, into: pinned.contains(key) ? visible : area)
            }
        }
    }
}

// MARK: - Live-test commands

/// Commands of the layout debug trigger (off unless the app starts with the debug flag).
/// Only these exact strings parse; no payload text ever reaches a card.
public enum LayoutDebugCommand: Equatable {
    case aside, restore, clear, shape
    case demo(Int)
    /// Sample media cards (2 images, a YouTube card and a video file card).
    case demoMedia
    /// Sample media cards that each fail a guard (private host, 404, unplayable id).
    case demoMediaBad

    public static func parse(_ text: String?) -> LayoutDebugCommand? {
        switch text {
        case "aside"?: return .aside
        case "restore"?: return .restore
        case "clear"?: return .clear
        case "shape"?: return .shape
        // Only these exact strings parse (same rule as every case above); never a payload.
        case "demo:media"?: return .demoMedia
        case "demo:media-bad"?: return .demoMediaBad
        case let text? where text.hasPrefix("demo:"):
            let digits = text.dropFirst(5)
            guard !digits.isEmpty, digits.count <= 2, digits.allSatisfy({ $0.isASCII && $0.isNumber }),
                  let count = Int(digits), (1...ScatterLayout.maxCards).contains(count)
            else { return nil }
            return .demo(count)
        default: return nil
        }
    }

    /// Neutral sample cards `demo-1...demo-N`, kinds cycling info, stat, bar, link, flow.
    public static func demoCards(count: Int) -> [HUDCard] {
        guard count > 0 else { return [] }
        return (1...count).map { index -> HUDCard in
            let id = "demo-\(index)"
            switch (index - 1) % 5 {
            case 0:
                return .info(InfoCard(id: id, title: "Sample note \(index)",
                                      body: ["Neutral sample text for the layout check.", "A second short line."]))
            case 1:
                return .stat(StatCard(id: id, title: "Sample stat \(index)", value: "\(index * 7)", label: "items"))
            case 2:
                return .bar(BarCard(id: id, title: "Sample chart \(index)",
                                    items: [BarItem(label: "North", value: 3), BarItem(label: "South", value: 5),
                                            BarItem(label: "East", value: 2)],
                                    caption: "Sample values"))
            case 3:
                return .link(LinkCard(id: id, title: "Sample link \(index)", url: "https://example.com",
                                      source: "example.com"))
            default:
                return .flow(FlowCard(id: id, title: "Sample flow \(index)",
                                      nodes: [FlowNode(id: "a", text: "Start"), FlowNode(id: "b", text: "Check"),
                                              FlowNode(id: "c", text: "Done")],
                                      edges: [FlowEdge(from: "a", to: "b"), FlowEdge(from: "b", to: "c")]))
            }
        }
    }

    /// Sample media cards `media-1...media-4` for the live layout check: a single photo, a
    /// 4-image gallery, a YouTube card and a direct video file card.
    public static func demoMediaCards() -> [HUDCard] {
        let gallery = [
            ImageItem(url: "https://upload.wikimedia.org/wikipedia/commons/3/36/Hopetoun_falls.jpg",
                      alt: "Waterfall in a fern forest"),
            ImageItem(url: "https://upload.wikimedia.org/wikipedia/commons/4/41/Sunflower_from_Silesia2.jpg",
                      alt: "Sunflower against a blue sky"),
            ImageItem(url: "https://upload.wikimedia.org/wikipedia/commons/1/16/Fox_-_British_Wildlife_Centre_%2817429406401%29.jpg",
                      alt: "Red fox"),
            ImageItem(url: "https://upload.wikimedia.org/wikipedia/commons/a/a8/Tour_Eiffel_Wikimedia_Commons.jpg",
                      alt: "Eiffel Tower from below"),
        ]
        return [
            .image(ImageCard(id: "media-1", title: "Sample photo",
                             url: "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg",
                             alt: "Cat portrait",
                             sourcePage: "https://commons.wikimedia.org/wiki/File:Cat03.jpg")),
            .image(ImageCard(id: "media-2", title: "Sample gallery", url: gallery[0].url, items: gallery)),
            .video(VideoCard(id: "media-3", title: "Sample YouTube video", youtubeID: "aqz-KE-bpKQ")),
            .video(VideoCard(id: "media-4", title: "Sample video file",
                             url: "https://upload.wikimedia.org/wikipedia/commons/transcoded/c/c0/Big_Buck_Bunny_4K.webm/Big_Buck_Bunny_4K.webm.360p.mpeg4.mov",
                             thumbnailURL: "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c0/Big_Buck_Bunny_4K.webm/960px--Big_Buck_Bunny_4K.webm.jpg",
                             sourcePage: "https://commons.wikimedia.org/wiki/File:Big_Buck_Bunny_4K.webm")),
        ]
    }

    /// Sample media cards `media-bad-1...3` that each fail a media guard (private host, HTTP
    /// 404, unplayable id) so the fallback paths can be checked live.
    public static func demoMediaBadCards() -> [HUDCard] {
        [
            .image(ImageCard(id: "media-bad-1", title: "Private host sample",
                             url: "https://\([192, 168, 1, 1].map(String.init).joined(separator: "."))/x.jpg")),  // a LAN router address
            .image(ImageCard(id: "media-bad-2", title: "Missing image sample",
                             url: "https://example.com/does-not-exist.jpg", alt: "Missing sample image")),
            .video(VideoCard(id: "media-bad-3", title: "Missing video sample", youtubeID: "zzzzzzzzzzz")),
        ]
    }
}
