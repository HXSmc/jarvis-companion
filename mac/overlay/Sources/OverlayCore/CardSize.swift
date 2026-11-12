import CoreGraphics

/// Card enlarge math: fit the natural size into `screenShare` of the visible screen, centred,
/// never distorted; a degenerate natural size falls back to an inset of the visible rect.
public enum CardSize {
    public static let screenShare: CGFloat = 0.8
    public static let fontScaleCap: CGFloat = 2.5
    /// A flow's text grows less than its width, so node labels get more room than on the tile.
    public static let flowFontScaleCap: CGFloat = 1.6

    public static func enlargedFrame(natural: CGSize, visible: CGRect) -> CGRect {
        fit(natural, in: visible, upscale: true)
    }

    /// The enlarged window around content already laid out at its enlarged size: centred, the
    /// content's own size (shrunk with its aspect only if it overflows the 80 % box). Scaling it up
    /// made the window wider than the card, and the card sat at the window's left (Mac live run 2:
    /// the phone diagram's centre at x 603 of 1440).
    public static func windowFrame(content: CGSize, visible: CGRect) -> CGRect {
        fit(content, in: visible, upscale: false)
    }

    private static func fit(_ natural: CGSize, in visible: CGRect, upscale: Bool) -> CGRect {
        guard natural.width > 0, natural.height > 0 else {
            return visible.insetBy(dx: visible.width * 0.1, dy: visible.height * 0.1)
        }
        let scale = min(screenShare * visible.width / natural.width,
                        screenShare * visible.height / natural.height, upscale ? .greatestFiniteMagnitude : 1)
        let size = CGSize(width: natural.width * scale, height: natural.height * scale)
        return CGRect(x: visible.midX - size.width / 2,
                      y: visible.midY - size.height / 2,
                      width: size.width,
                      height: size.height)
    }
}

/// Which card is enlarged; `max(_:)` returns the previously enlarged id so the caller can
/// shrink it back.
public struct EnlargeState: Equatable {
    public private(set) var id: String?

    public init() {}

    /// The previously enlarged id when a different card takes over, else nil.
    public mutating func max(_ id: String) -> String? {
        let previous = self.id == id ? nil : self.id
        self.id = id
        return previous
    }

    /// While a card is enlarged the idle clear waits (the owner is reading it).
    public var holdsIdle: Bool { id != nil }

    public mutating func restore() {
        id = nil
    }

    public mutating func dropped(ids: [String]) {
        if let current = id, ids.contains(current) { id = nil }
    }
}

/// Flow edge labels placed so no two overlap and none sits on a node: each label tries points
/// along its edge (middle first) and is left out when none is free. Mac live run 2: the six
/// labels fanning out of "Processor (SoC)" all sat at their edges' middles and printed over each
/// other ("image/touch/readings/data").
public enum FlowEdgeLabels {
    public static let fractions: [CGFloat] = [0.5, 0.4, 0.6, 0.3, 0.7]

    /// Box of a label in the monospaced micro font (11 pt x scale): about 0.62 em per character,
    /// 1.3 em high.
    public static func size(_ label: String, scale: CGFloat) -> CGSize {
        CGSize(width: CGFloat(label.count) * 11 * 0.62 * scale, height: 11 * 1.3 * scale)
    }

    /// The left-centre anchor of each label (drawn leading, `gap` right of its edge), nil = left out.
    public static func place(_ edges: [(from: CGRect, to: CGRect, size: CGSize)], nodes: [CGRect],
                             gap: CGFloat) -> [CGPoint?] {
        var placed: [CGRect] = []
        return edges.map { e in
            for t in fractions {
                let p = CGPoint(x: e.from.midX + (e.to.midX - e.from.midX) * t + gap,
                                y: e.from.midY + (e.to.midY - e.from.midY) * t)
                let box = CGRect(x: p.x, y: p.y - e.size.height / 2, width: e.size.width, height: e.size.height)
                if placed.contains(where: { $0.intersects(box) }) || nodes.contains(where: { $0.intersects(box) }) {
                    continue
                }
                placed.append(box)
                return p
            }
            return nil
        }
    }
}
