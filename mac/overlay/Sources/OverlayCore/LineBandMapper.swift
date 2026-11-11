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

    /// The longest chain of marks on one evenly spaced line grid, OCR outliers skipped. VS Code's
    /// sticky-scroll headers show ancestor line numbers (587, 618, 678, 687, 689) on the gutter rows
    /// above the scrolling lines (692...732): a fit through them drew line 710's band two rows low
    /// (Mac run, 33a). Pitch = median step between consecutive line numbers. A chain of fewer than
    /// 2 marks returns `marks` unchanged.
    public static func evenRun(_ marks: [(line: Int, midY: CGFloat)]) -> [(line: Int, midY: CGFloat)] {
        let sorted = marks.sorted { $0.line < $1.line }
        let pairs = zip(sorted, sorted.dropFirst()).filter { $0.1.line > $0.0.line }
        let unit = pairs.filter { $0.1.line - $0.0.line == 1 }.map { $0.1.midY - $0.0.midY }
        let steps = (unit.isEmpty ? pairs.map { ($0.1.midY - $0.0.midY) / CGFloat($0.1.line - $0.0.line) } : unit).sorted()
        guard !steps.isEmpty else { return marks }
        let pitch = steps[steps.count / 2]
        guard pitch != 0 else { return marks }
        let tolerance = max(2, abs(pitch) * 0.25)
        var best: [(line: Int, midY: CGFloat)] = []
        for start in sorted.indices {
            var chain = [sorted[start]]
            for mark in sorted[(start + 1)...] {
                let last = chain[chain.count - 1]
                if mark.line > last.line,
                   abs(mark.midY - last.midY - pitch * CGFloat(mark.line - last.line)) <= tolerance {
                    chain.append(mark)
                }
            }
            if chain.count > best.count { best = chain }
        }
        return best.count >= 2 ? best : marks
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

    /// Apps whose window title may pick the gutter pass (a browser tab titled "overlay.py" may not).
    public static let editorApps: Set<String> = ["Code", "Visual Studio Code", "Cursor", "Xcode"]

    /// Front-most editor window (`editorApps`) whose title names `file` (its last path part as a
    /// whole word, e.g. "● overlay.py — repo"), or nil. `windows` front to back.
    public static func editorWindow(file: String, windows: [(app: String, title: String, frame: CGRect)]) -> CGRect? {
        let name = file.split(separator: "/").last.map(String.init) ?? ""
        guard !name.isEmpty else { return nil }
        let wordChar: (Character) -> Bool = { $0.isLetter || $0.isNumber || "._-".contains($0) }
        return windows.first { window in
            guard editorApps.contains(window.app) else { return false }
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

/// The `lines` retry loop without the screen: `pause(attempt)` then `pass(attempt)`, up to `tries`
/// times, until `done` accepts a pass. A cancelled task (the owner cleared the screen) stops before
/// the next pass and returns nil, so nothing is drawn or reported after the clear.
public enum BandRetry {
    public static func run<P>(tries: Int, pause: (Int) async -> Void, pass: (Int) async -> P,
                              done: (P) -> Bool) async -> (pass: P, attempt: Int)? {
        var last: (pass: P, attempt: Int)?
        for attempt in 1...max(tries, 1) {
            await pause(attempt)
            if Task.isCancelled { return nil }
            let result = await pass(attempt)
            last = (result, attempt)
            if done(result) { break }
        }
        return Task.isCancelled ? nil : last
    }

    /// Index of the wanted range a drawn band belongs to (a band on the last try may be the visible
    /// part of its range), for its label: an exact match, else the range it overlaps most (the first on
    /// a tie); nil when none overlaps.
    public static func rangeIndex(of band: ClosedRange<Int>, in ranges: [ClosedRange<Int>]) -> Int? {
        if let exact = ranges.firstIndex(of: band) { return exact }
        var best: (index: Int, rows: Int)?
        for (index, range) in ranges.enumerated() where range.overlaps(band) {
            let rows = min(range.upperBound, band.upperBound) - max(range.lowerBound, band.lowerBound) + 1
            if rows > (best?.rows ?? 0) { best = (index, rows) }
        }
        return best?.index
    }
}
