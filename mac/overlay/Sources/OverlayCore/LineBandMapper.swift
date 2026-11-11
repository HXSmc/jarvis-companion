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

    /// Median step per line among consecutive line numbers (evenRun's pitch, standalone):
    /// unit steps where they exist, else the step divided by the line gap. nil = no pitch.
    static func unitPitch(_ marks: [(line: Int, midY: CGFloat)]) -> CGFloat? {
        let sorted = marks.sorted { $0.line < $1.line }
        let pairs = zip(sorted, sorted.dropFirst()).filter { $1.line > $0.line }
        let unit = pairs.filter { $1.line - $0.line == 1 }.map { $1.midY - $0.midY }
        let steps = (unit.isEmpty ? pairs.map { ($1.midY - $0.midY) / CGFloat($1.line - $0.line) } : unit).sorted()
        guard !steps.isEmpty else { return nil }
        let pitch = steps[steps.count / 2]
        return pitch == 0 ? nil : pitch
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
        /// range is only partly in view, not the last try) | off_clip (band outside the window) |
        /// folded (the wanted lines sit in a range the editor folded away).
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
    /// (`partial`) the visible part of a range that is only partly in view is drawn too. A short
    /// file (2-4 lines starting at 1) gets the same treatment even without a 5-digit column (B4).
    public static func fit(ranges: [ClosedRange<Int>], digits: [(line: Int, centre: CGPoint, right: CGFloat)],
                           window: CGRect, partial: Bool) -> Pass {
        let gutter: (marks: [(line: Int, midY: CGFloat)], right: CGFloat)
        var shortMarks: [(line: Int, midY: CGFloat)]?
        if let normal = ShapeTrack.gutterMarks(digits: digits, window: window) {
            gutter = normal
        } else if let short = shortGutter(digits: digits, window: window) {
            gutter = short
            shortMarks = short.marks
        } else {
            return Pass(reason: "no_gutter")
        }
        // B5: every in-window digit of the gutter column, not just evenRun's run — a fold hides
        // one side of the column from the run, and a wrapped line's rows never appear in it.
        let inWindow = digits.filter { window.contains($0.centre) }
        let runLines = Set(gutter.marks.map(\.line))
        let raw: [(line: Int, midY: CGFloat)]
        if let shortMarks {
            raw = shortMarks.sorted { $0.line < $1.line }
        } else if let columnLeftX = inWindow.filter({ runLines.contains($0.line) }).map({ $0.centre.x }).min() {
            raw = inWindow.filter { abs($0.centre.x - columnLeftX) <= 12 }
                .map { (line: $0.line, midY: $0.centre.y) }.sorted { $0.line < $1.line }
        } else {
            raw = []
        }
        let lines = gutter.marks.map(\.line)
        guard let mapper = LineBandMapper(marks: gutter.marks), let low = lines.min(), let high = lines.max()
        else { return Pass(reason: "no_fit") }
        let seen = low...high
        let x = gutter.right + 4
        let width = window.maxX - 8 - x
        guard width > 0 else { return Pass(reason: "narrow", seen: seen) }
        let full = ranges.filter { seen.contains($0.lowerBound) && seen.contains($0.upperBound) }
        let parts = ranges.compactMap { $0.overlaps(seen) ? $0.clamped(to: seen) : nil }
        let wanted = !full.isEmpty ? full : (partial ? parts : [])
        if wanted.isEmpty {
            if ranges.contains(where: { isFolded($0, raw: raw) }) { return Pass(reason: "folded", seen: seen) }
            return Pass(reason: parts.isEmpty ? "off_view" : "partial", seen: seen)
        }
        let bands = wanted.compactMap { range -> (lines: ClosedRange<Int>, rect: CGRect)? in
            ShapeTrack.placeBand(lines: range, marks: gutter.marks, x: x, width: width, clip: window)
                .map { (lines: range, rect: wrapExtended($0, range: range, mapper: mapper, raw: raw)) }
        }
        return Pass(bands: bands, reason: bands.isEmpty ? "off_clip" : "ok", seen: seen)
    }

    /// B4: the whole gutter of a 2-4 line file — every in-window digit of the left-most column,
    /// accepted only when its lines are 1...n (consecutive from 1, no repeats) and evenly spaced.
    private static func shortGutter(digits: [(line: Int, centre: CGPoint, right: CGFloat)],
                                    window: CGRect) -> (marks: [(line: Int, midY: CGFloat)], right: CGFloat)? {
        let sorted = digits.filter { window.contains($0.centre) }.sorted { $0.centre.x < $1.centre.x }
        guard let first = sorted.first else { return nil }
        let group = sorted.filter { $0.centre.x - first.centre.x <= 12 }
        let marks = group.map { (line: $0.line, midY: $0.centre.y) }
        let lineNumbers = marks.map(\.line).sorted()
        guard (2...4).contains(group.count), lineNumbers == Array(1...group.count),
              LineBandMapper.evenRun(marks).count == group.count else { return nil }
        return (LineBandMapper.evenRun(marks), group.map(\.right).max() ?? 0)
    }

    /// B5a: the range is folded away — no raw mark inside it, and the raw marks just below and
    /// above it (neighbours in line order) sit within 1.5 rows of each other, so the editor
    /// skipped the range's rows instead of scrolling them off screen.
    private static func isFolded(_ range: ClosedRange<Int>, raw: [(line: Int, midY: CGFloat)]) -> Bool {
        guard let pitch = LineBandMapper.unitPitch(raw),
              !raw.contains(where: { range.contains($0.line) }) else { return false }
        let sorted = raw.sorted { $0.line < $1.line }
        guard let below = sorted.last(where: { $0.line < range.lowerBound }),
              let above = sorted.first(where: { $0.line > range.upperBound }) else { return false }
        return above.line - below.line > 1 && abs(above.midY - below.midY) <= 1.5 * abs(pitch)
    }

    /// B5b: a wrapped line spans more rows than the fit knows; when the next raw mark sits more
    /// than 1.5 rows from the fitted y of the range's last line, stretch the band's edge on that
    /// side up to half a row short of the next mark's centre.
    private static func wrapExtended(_ rect: CGRect, range: ClosedRange<Int>, mapper: LineBandMapper,
                                     raw: [(line: Int, midY: CGFloat)]) -> CGRect {
        let pitch = LineBandMapper.unitPitch(raw) ?? mapper.a
        guard pitch != 0, let next = raw.first(where: { $0.line > range.upperBound }) else { return rect }
        let yLast = mapper.b + mapper.a * CGFloat(range.upperBound)
        guard (next.midY - yLast) / pitch > 1.5 else { return rect }
        let edge = next.midY - pitch / 2
        return next.midY > rect.midY
            ? CGRect(x: rect.minX, y: min(rect.minY, edge), width: rect.width, height: abs(edge - rect.minY))
            : CGRect(x: rect.minX, y: min(edge, rect.maxY), width: rect.width, height: abs(rect.maxY - edge))
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
