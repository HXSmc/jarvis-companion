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
