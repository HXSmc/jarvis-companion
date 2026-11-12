import CoreGraphics

/// Card enlarge math: fit the natural size into `screenShare` of the visible screen, centred,
/// never distorted; a degenerate natural size falls back to an inset of the visible rect.
public enum CardSize {
    public static let screenShare: CGFloat = 0.8
    public static let fontScaleCap: CGFloat = 2.5
    /// A flow's text grows less than its width, so node labels get more room than on the tile.
    public static let flowFontScaleCap: CGFloat = 1.6

    public static func enlargedFrame(natural: CGSize, visible: CGRect) -> CGRect {
        guard natural.width > 0, natural.height > 0 else {
            return visible.insetBy(dx: visible.width * 0.1, dy: visible.height * 0.1)
        }
        let scale = min(screenShare * visible.width / natural.width,
                        screenShare * visible.height / natural.height)
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
