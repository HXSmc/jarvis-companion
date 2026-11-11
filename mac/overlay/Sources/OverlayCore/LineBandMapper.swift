import Foundation

/// Least-squares fit of editor-gutter OCR marks: y = a * line + b (screen points).
/// Used to map changed-line ranges from the brain to highlight bands in the editor.
public struct LineBandMapper: Equatable {
    /// Points per line (negative when line numbers grow upward).
    public let a: CGFloat
    /// y of line 0.
    public let b: CGFloat

    /// nil when fewer than 2 distinct lines are marked (no fit possible).
    public init?(marks: [(line: Int, midY: CGFloat)]) {
        guard marks.count >= 2, Set(marks.map(\.line)).count >= 2 else { return nil }
        let n = CGFloat(marks.count)
        let xs = marks.map { CGFloat($0.line) }
        let ys = marks.map { $0.midY }
        let sx = xs.reduce(0, +)
        let sy = ys.reduce(0, +)
        let sxx = xs.reduce(0) { $0 + $1 * $1 }
        let sxy = zip(xs, ys).reduce(0) { $0 + $1.0 * $1.1 }
        let denom = n * sxx - sx * sx
        guard abs(denom) > 1e-9 else { return nil }
        a = (n * sxy - sx * sy) / denom
        b = (sy - a * sx) / n
    }

    /// Highlight band for `lines` spanning the text area from x to x+width:
    /// y from y(start) - h/2 to y(end) + h/2, h = |a| (one line height).
    public func band(for lines: ClosedRange<Int>, x: CGFloat, width: CGFloat) -> CGRect {
        let h = abs(a)
        let top = b + CGFloat(lines.lowerBound) * a - h / 2
        let bottom = b + CGFloat(lines.upperBound) * a + h / 2
        return CGRect(x: x, y: min(top, bottom), width: width, height: abs(bottom - top))
    }
}

/// One gutter-OCR pass for a `lines` event: the bands to draw, or a short reason code for none.
/// Codes and line numbers only (no text, no paths): they go to the debug page as they are.
public enum LineBandFit {
    public struct Pass {
        public var bands: [(lines: ClosedRange<Int>, rect: CGRect)] = []
        /// ok | no_gutter | no_fit | narrow | off_view (no wanted line in view) | partial (a wanted
        /// range is only partly in view, not the last try) | off_clip (band outside the window).
        public var reason: String
        /// Line numbers the gutter showed.
        public var seen: ClosedRange<Int>?

        public init(bands: [(lines: ClosedRange<Int>, rect: CGRect)] = [], reason: String, seen: ClosedRange<Int>? = nil) {
            self.bands = bands
            self.reason = reason
            self.seen = seen
        }
    }

    /// Bands from gutter digits (points, `right` = the digit box's right edge) inside `window`.
    /// Never extrapolates past the lines on screen: a pass taken before the editor scrolled says
    /// off_view (retry), not a band far off screen. A range fully in view wins; on the last try
    /// (`partial`) the visible part of a range that is only partly in view is drawn too.
    public static func fit(ranges: [ClosedRange<Int>], digits: [(line: Int, centre: CGPoint, right: CGFloat)],
                           window: CGRect, partial: Bool) -> Pass {
        guard let gutter = ShapeTrack.gutterMarks(digits: digits, window: window) else { return Pass(reason: "no_gutter") }
        let lines = gutter.marks.map(\.line)
        guard LineBandMapper(marks: gutter.marks) != nil, let low = lines.min(), let high = lines.max()
        else { return Pass(reason: "no_fit") }
        let seen = low...high
        let x = gutter.right + 4
        let width = window.maxX - 8 - x
        guard width > 0 else { return Pass(reason: "narrow", seen: seen) }
        let full = ranges.filter { seen.contains($0.lowerBound) && seen.contains($0.upperBound) }
        let parts = ranges.compactMap { $0.overlaps(seen) ? $0.clamped(to: seen) : nil }
        let wanted = !full.isEmpty ? full : (partial ? parts : [])
        if wanted.isEmpty { return Pass(reason: parts.isEmpty ? "off_view" : "partial", seen: seen) }
        let bands = wanted.compactMap { range -> (lines: ClosedRange<Int>, rect: CGRect)? in
            ShapeTrack.placeBand(lines: range, marks: gutter.marks, x: x, width: width, clip: window).map { (lines: range, rect: $0) }
        }
        return Pass(bands: bands, reason: bands.isEmpty ? "off_clip" : "ok", seen: seen)
    }

    /// Front-most window whose title names `file` (its last path part as a whole word, e.g.
    /// "● overlay.py — repo"), or nil. `windows` front to back.
    public static func editorWindow(file: String, windows: [(title: String, frame: CGRect)]) -> CGRect? {
        let name = file.split(separator: "/").last.map(String.init) ?? ""
        guard !name.isEmpty else { return nil }
        let wordChar: (Character) -> Bool = { $0.isLetter || $0.isNumber || "._-".contains($0) }
        return windows.first { window in
            var rest = window.title[...]
            while let hit = rest.range(of: name) {
                let before = hit.lowerBound == window.title.startIndex ? nil : window.title[window.title.index(before: hit.lowerBound)]
                let after = hit.upperBound == window.title.endIndex ? nil : window.title[hit.upperBound]
                if !(before.map(wordChar) ?? false), !(after.map(wordChar) ?? false) { return true }
                rest = window.title[hit.upperBound...]
            }
            return false
        }?.frame
    }
}
