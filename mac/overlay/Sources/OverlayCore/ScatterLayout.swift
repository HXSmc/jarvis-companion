import Foundation
import CoreGraphics

/// Card placement over the whole visible screen: seeded, loose and layered (3x3 zones, least
/// occupied first), never on a keep-out (orb, highlight boxes), pairwise overlap at most 30 %
/// of the smaller card. All rects are top-left global points, the space of `HUDShape.rect`.
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
    static let candidatesPerZone = 24

    // Orb geometry, mirrors OverlayRootView.swift:26-30 (`.padding(24)`, bottom-right) and
    // OrbView.swift:53 (44x44). The banner band covers the status capsule above it.
    public static let orbSize: CGFloat = 44
    public static let orbPadding: CGFloat = 24
    public static let bannerBand: CGFloat = 40

    public static let chipSize = CGSize(width: 200, height: 28)
    public static let chipGap: CGFloat = 6

    public enum Placement: Equatable {
        case at(CGRect)
        /// Only overlap is in the way: retire the oldest card and call again.
        case retireOldest
        /// Every spot touches a keep-out: the card is not shown for now.
        case noRoom
    }

    /// Places one card. `existing` is oldest first. With `allowRetire` false (parked cards are
    /// never retired) the least-overlap spot wins even past the overlap caps.
    public static func place(size: CGSize, key: String, visible: CGRect, existing: [CGRect],
                             keepOut: [CGRect], allowRetire: Bool = true) -> Placement {
        let area = insetArea(visible)
        guard area.width > 0, area.height > 0 else { return .noRoom }
        let size = CGSize(width: min(size.width, area.width), height: min(size.height, area.height))
        var rng = SplitMix64(state: seed(key))
        var occupied = [Int](repeating: 0, count: 9)
        for frame in existing { occupied[zone(of: CGPoint(x: frame.midX, y: frame.midY), in: area)] += 1 }
        // How much of each existing card the others already cover.
        let covered = existing.indices.map { index in
            existing.indices.reduce(CGFloat(0)) { $0 + ($1 == index ? 0 : overlapArea(existing[index], existing[$1])) }
        }
        let tieBreak = (0..<9).map { _ in rng.next() }
        let zones = (0..<9).sorted { (occupied[$0], tieBreak[$0]) < (occupied[$1], tieBreak[$1]) }
        let zoneSize = CGSize(width: area.width / 3, height: area.height / 3)
        var best: (frame: CGRect, score: CGFloat)?
        var clearOfKeepOut = false
        for zone in zones {
            let zoneOrigin = CGPoint(x: area.minX + CGFloat(zone % 3) * zoneSize.width,
                                     y: area.minY + CGFloat(zone / 3) * zoneSize.height)
            for _ in 0..<candidatesPerZone {
                let centre = CGPoint(x: zoneOrigin.x + rng.unit() * zoneSize.width,
                                     y: zoneOrigin.y + rng.unit() * zoneSize.height)
                let frame = clamp(CGRect(x: centre.x - size.width / 2, y: centre.y - size.height / 2,
                                         width: size.width, height: size.height), into: area)
                if keepOut.contains(where: { $0.intersects(frame) }) { continue }
                clearOfKeepOut = true
                guard let score = overlapScore(frame, existing: existing, covered: covered, capped: allowRetire)
                else { continue }
                if score < (best?.score ?? .infinity) { best = (frame, score) }
            }
            if let best, best.score == 0 { return .at(best.frame) }
        }
        if let best { return .at(best.frame) }
        return clearOfKeepOut && !existing.isEmpty ? .retireOldest : .noRoom
    }

    /// Sum of overlap areas with the existing cards; nil when `capped` and a pair passes
    /// `maxOverlap` of the smaller card or any card would pass `maxCoverage`.
    static func overlapScore(_ frame: CGRect, existing: [CGRect], covered: [CGFloat], capped: Bool) -> CGFloat? {
        var total: CGFloat = 0
        for (index, other) in existing.enumerated() {
            let overlap = overlapArea(frame, other)
            guard overlap > 0 else { continue }
            if capped {
                if overlap / min(area(frame), area(other)) > maxOverlap { return nil }
                if (covered[index] + overlap) / area(other) > maxCoverage { return nil }
            }
            total += overlap
        }
        if capped, area(frame) > 0, total / area(frame) > maxCoverage { return nil }
        return total
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
        var column: CGFloat = 0
        var y = area.minY
        for key in keys {
            if y + chipSize.height > area.maxY, y > area.minY {
                column += 1
                y = area.minY
            }
            let step = column * (chipSize.width + chipGap)
            let x = orbOnRight ? area.minX + step : area.maxX - chipSize.width - step
            frames[key] = CGRect(x: x, y: y, width: chipSize.width, height: chipSize.height)
            y += chipSize.height + chipGap
        }
        return frames
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
    /// card comes or goes; only its own replacement, a screen change (reclamp) or removal does.
    public struct LayoutState: Equatable {
        public enum Mode: Equatable { case normal, aside }

        public private(set) var mode: Mode = .normal
        /// Stored frame per card key; kept while aside, so restore puts each card back.
        public private(set) var frames: [String: CGRect] = [:]
        /// Placed keys, oldest first.
        public private(set) var order: [String] = []

        public init() {}

        /// Keys shown as chips: every placed card while aside, none otherwise.
        public var chipKeys: [String] { mode == .aside ? order : [] }

        public mutating func aside() { mode = .aside }
        public mutating func restore() { mode = .normal }
        public mutating func clear() { self = LayoutState() }

        /// Places a measured card against the stored frames. Returns the keys retired to make
        /// room, oldest first. The card stays unplaced when only a keep-out is in the way.
        /// While aside nothing is retired: the card takes the least-overlap spot.
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
                    guard let oldest = order.first else { return retired }
                    retired.append(oldest)
                    cardRemoved(oldest)
                case .noRoom:
                    return retired
                }
            }
        }

        /// A card replaced in place keeps its origin; only its new size is clamped.
        public mutating func cardResized(_ key: String, size: CGSize, visible: CGRect) {
            guard let old = frames[key] else { return }
            frames[key] = ScatterLayout.clamp(CGRect(origin: old.origin, size: size), into: ScatterLayout.insetArea(visible))
        }

        public mutating func cardRemoved(_ key: String) {
            frames[key] = nil
            order.removeAll { $0 == key }
        }

        /// Forgets every card not in `live` (cap, idle clear, clear).
        public mutating func keep(_ live: Set<String>) {
            guard order.contains(where: { !live.contains($0) }) else { return }
            order.removeAll { !live.contains($0) }
            frames = frames.filter { live.contains($0.key) }
        }

        public mutating func reclamp(visible: CGRect) {
            frames = ScatterLayout.reclamp(frames: frames, visible: visible)
        }
    }
}

// MARK: - Live-test commands

/// Commands of the layout debug trigger (off unless the app starts with the debug flag).
/// Only these exact strings parse; no payload text ever reaches a card.
public enum LayoutDebugCommand: Equatable {
    case aside, restore, clear, shape
    case demo(Int)

    public static func parse(_ text: String?) -> LayoutDebugCommand? {
        switch text {
        case "aside"?: return .aside
        case "restore"?: return .restore
        case "clear"?: return .clear
        case "shape"?: return .shape
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
}
