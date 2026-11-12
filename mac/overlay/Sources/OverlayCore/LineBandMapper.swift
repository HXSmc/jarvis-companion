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

    /// B4: the whole gutter of a 2-4 line file — the left-most column (digits within 12 pt in x)
    /// whose lines are 1...n (consecutive from 1, no repeats) and evenly spaced. Columns that fail
    /// are skipped, as gutterMarks does: VS Code's account badge "1" and the status bar's "0 0" sit
    /// left of the gutter (Mac run 2, B4: a 3-line file said no_gutter on every try).
    private static func shortGutter(digits: [(line: Int, centre: CGPoint, right: CGFloat)],
                                    window: CGRect) -> (marks: [(line: Int, midY: CGFloat)], right: CGFloat)? {
        let sorted = digits.filter { window.contains($0.centre) }.sorted { $0.centre.x < $1.centre.x }
        var start = 0
        while start < sorted.count {
            var end = start
            while end + 1 < sorted.count, sorted[end + 1].centre.x - sorted[start].centre.x <= 12 { end += 1 }
            let group = sorted[start...end]
            let marks = group.map { (line: $0.line, midY: $0.centre.y) }
            if (2...4).contains(group.count), marks.map(\.line).sorted() == Array(1...group.count),
               LineBandMapper.evenRun(marks).count == group.count {
                return (LineBandMapper.evenRun(marks), group.map(\.right).max() ?? 0)
            }
            start = end + 1
        }
        return nil
    }

    /// Two passes show the same boxes: the same ranges, every band edge within 2 pt (an OCR miss at
    /// the gutter's edge may change `seen` without moving a box, so `seen` is not compared).
    /// A pass taken before the editor's scroll landed differs from the one after it (Mac run 2, B5:
    /// the box for line 149 was drawn where 149 sat before `code -g` centred it, 9 rows low).
    public static func sameBands(_ a: Pass, _ b: Pass) -> Bool {
        a.bands.count == b.bands.count
            && zip(a.bands, b.bands).allSatisfy { x, y in
                x.lines == y.lines && abs(x.rect.minY - y.rect.minY) <= 2 && abs(x.rect.maxY - y.rect.maxY) <= 2
            }
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
    /// `same` (optional): an accepted pass also needs the next pass to agree with it (the screen
    /// settled); a pass that differs becomes the new candidate. The last try is taken as it is.
    public static func run<P>(tries: Int, pause: (Int) async -> Void, pass: (Int) async -> P,
                              done: (P) -> Bool, same: ((P, P) -> Bool)? = nil) async -> (pass: P, attempt: Int)? {
        var last: (pass: P, attempt: Int)?
        let tries = max(tries, 1)
        for attempt in 1...tries {
            await pause(attempt)
            if Task.isCancelled { return nil }
            let result = await pass(attempt)
            let settled = same.map { same in last.map { done($0.pass) && same($0.pass, result) } ?? false } ?? true
            last = (result, attempt)
            if done(result) && (settled || attempt == tries) { break }
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

#if canImport(Vision)
import CoreGraphics
import Vision

/// Gutter line numbers from a screen shot. Vision's text reader drops a lone glyph: lines 1-9 never
/// came back, so a 3-line file said no_gutter and line 6 said off_view with the line on screen (Mac
/// run 3, B4 and B5 fold: `seen` always started at 10). A second read doubles each gutter cell that
/// pass 1 left unread ("6" -> "66", "10" -> "1010") and accepts only a text that is two equal halves.
public enum GutterOCR {
    /// Text observations as top-left pixel boxes [x0, y0, x1, y1].
    public static func text(in image: CGImage) -> [(text: String, box: [Double])] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.minimumTextHeight = 0.008
        try? VNImageRequestHandler(cgImage: image).perform([request])
        let w = Double(image.width), h = Double(image.height)
        var out: [(String, [Double])] = []
        for observation in request.results ?? [] {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let bb = observation.boundingBox
            let x0 = bb.minX * w, y0 = (1 - bb.maxY) * h
            out.append((text, [x0, y0, x0 + bb.width * w, y0 + bb.height * h]))
        }
        return out
    }

    /// Line numbers (pixel boxes) in `window` (pixels): pass 1's 1-5 digit texts plus the cells
    /// pass 1 left unread. `observations`: pass 1 over the same image.
    public static func lineDigits(in image: CGImage, observations: [(text: String, box: [Double])],
                                  window: CGRect) -> [(line: Int, box: [Double])] {
        let read = observations.compactMap { o in ShapeTrack.lineNumber(o.text).map { (line: $0, box: o.box) } }
        let cells = cellsToRead(in: image, observations: observations, window: window)
        guard !cells.isEmpty else { return read }
        return read + readDoubled(cells, in: image)
    }

    /// Two equal halves, spaces ignored: "66" -> 6, "8 8" -> 8, "1010" -> 10; else nil.
    public static func doubled(_ text: String) -> Int? {
        let s = text.filter { $0 != " " }
        guard s.count % 2 == 0, (1...5).contains(s.count / 2), s.allSatisfy(\.isASCII), s.allSatisfy(\.isNumber),
              s.prefix(s.count / 2) == s.suffix(s.count / 2) else { return nil }
        return Int(s.prefix(s.count / 2))
    }

    // MARK: cells

    private static let maxCells = 80

    /// Tight pixel boxes of the unread gutter numbers. With a read column (5+ numbers, as
    /// gutterMarks): the rows above its top number (1-9 are always above 10). Without one (a short
    /// file): the gutter cell of every row of text that starts at a shared left edge.
    static func cellsToRead(in image: CGImage, observations: [(text: String, box: [Double])],
                            window: CGRect) -> [CGRect] {
        guard let px = Pixels(image) else { return [] }
        let inWindow = observations.filter { window.contains(CGPoint(x: ($0.box[0] + $0.box[2]) / 2, y: ($0.box[1] + $0.box[3]) / 2)) }
        guard !inWindow.isEmpty else { return [] }
        let heights = inWindow.map { $0.box[3] - $0.box[1] }.sorted()
        let textH = CGFloat(heights[heights.count / 2])
        let box: ([Double]) -> CGRect = { CGRect(x: $0[0], y: $0[1], width: $0[2] - $0[0], height: $0[3] - $0[1]) }
        let taken = inWindow.map { box($0.box) }
        let numbers = inWindow.filter { ShapeTrack.lineNumber($0.text) != nil }.map { box($0.box) }
        var cells: [CGRect] = []
        // A cell where pass 1 already read a number, or one already planned (an indented row group
        // finds the same gutter), is not read again. Other text there ("@6": a gutter icon read with
        // the digit) does not block it: the cell's crop keeps only grey ink.
        let free: (CGRect) -> Bool = { cell in
            !numbers.contains { $0.contains(CGPoint(x: cell.midX, y: cell.midY)) } && !cells.contains { $0.intersects(cell) }
        }
        let digits = inWindow.compactMap { o in
            ShapeTrack.lineNumber(o.text).map { (line: $0, centre: CGPoint(x: (o.box[0] + o.box[2]) / 2, y: (o.box[1] + o.box[3]) / 2),
                                                 right: CGFloat(o.box[2]), width: CGFloat(o.box[2] - o.box[0]), count: o.text.count) }
        }
        if let column = ShapeTrack.gutterMarks(digits: digits.map { (line: $0.line, centre: $0.centre, right: $0.right) },
                                               window: window),
           let pitch = LineBandMapper.unitPitch(column.marks), pitch > 0,
           let top = column.marks.min(by: { $0.midY < $1.midY }) {
            let widths = digits.filter { abs($0.right - column.right) <= 6 }.map { $0.width / CGFloat($0.count) }.sorted()
            let glyph = widths.isEmpty ? textH * 0.6 : widths[widths.count / 2]
            var y = top.midY - pitch
            while y - pitch / 2 >= window.minY, cells.count < maxCells {
                if let cell = px.numberCell(right: column.right, y: y, half: pitch * 0.4, glyph: glyph), free(cell) { cells.append(cell) }
                y -= pitch
            }
            return cells
        }
        // No column: rows of text sharing a left edge (within 6 px), at least 2 rows per edge.
        var lefts: [[CGRect]] = []
        for box in taken.filter({ $0.minX > window.minX + textH }).sorted(by: { $0.minX < $1.minX }) {
            if let first = lefts.last?.first, box.minX - first.minX <= 6 { lefts[lefts.count - 1].append(box) } else { lefts.append([box]) }
        }
        for rows in lefts where rows.count >= 2 {
            let codeLeft = rows.map(\.minX).min() ?? 0
            let glyph = textH * 0.6
            let rights = rows.compactMap { px.inkRight(from: codeLeft - textH * 0.25, to: codeLeft - textH * 8, y: $0.midY, half: textH * 0.5) }.sorted()
            guard !rights.isEmpty else { continue }
            let right = rights[rights.count / 2]
            for row in rows where cells.count < maxCells {
                if let cell = px.numberCell(right: right, y: row.midY, half: textH * 0.6, glyph: glyph), free(cell) { cells.append(cell) }
            }
        }
        return cells
    }

    /// One read of all cells, each drawn twice side by side, cells far apart on one line.
    private static func readDoubled(_ cells: [CGRect], in image: CGImage) -> [(line: Int, box: [Double])] {
        let height = Int(cells.map(\.height).max() ?? 1)
        let gap = max(30, height)
        let pad = 40
        var spans: [(range: ClosedRange<Int>, cell: CGRect, crop: CGImage)] = []
        var x = pad
        for cell in cells {
            guard let crop = image.cropping(to: cell.integral) else { continue }
            let span = x...(x + 2 * crop.width + 2)
            spans.append((span, cell, crop))
            x = span.upperBound + gap
        }
        guard !spans.isEmpty,
              let ctx = CGContext(data: nil, width: x + pad, height: height + 2 * pad, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return [] }
        // Paint the composite with the first cell's corner colour (the gutter background).
        if let corner = Pixels(spans[0].crop)?.rgb(0, 0) {
            ctx.setFillColor(CGColor(red: corner.0, green: corner.1, blue: corner.2, alpha: 1))
            ctx.fill(CGRect(x: 0, y: 0, width: x + pad, height: height + 2 * pad))
        }
        for s in spans {
            let y = pad + (height - s.crop.height) / 2
            ctx.draw(s.crop, in: CGRect(x: s.range.lowerBound, y: y, width: s.crop.width, height: s.crop.height))
            ctx.draw(s.crop, in: CGRect(x: s.range.lowerBound + s.crop.width + 2, y: y, width: s.crop.width, height: s.crop.height))
        }
        guard let composite = ctx.makeImage() else { return [] }
        // Each character goes to the cell under it, so two cells read as one text ("66 55") still count.
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        try? VNImageRequestHandler(cgImage: composite).perform([request])
        var perCell: [Int: String] = [:]
        for observation in request.results ?? [] {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string
            for index in text.indices where text[index] != " " {
                guard let hit = try? candidate.boundingBox(for: index..<text.index(after: index)) else { continue }
                let mid = Int(hit.boundingBox.midX * Double(composite.width))
                guard let cell = spans.firstIndex(where: { $0.range.contains(mid) }) else { continue }
                perCell[cell, default: ""].append(text[index])
            }
        }
        let found = perCell.compactMapValues(doubled)
        return found.sorted { $0.key < $1.key }.map { hit in
            let c = spans[hit.key].cell
            return (line: hit.value, box: [Double(c.minX), Double(c.minY), Double(c.maxX), Double(c.maxY)])
        }
    }
}

/// RGBA8 copy of an image, rows top-down, for the gutter ink scan.
struct Pixels {
    let width: Int, height: Int
    private let data: [UInt8]

    init?(_ image: CGImage) {
        width = image.width; height = image.height
        var buffer = [UInt8](repeating: 0, count: width * height * 4)
        let ok = buffer.withUnsafeMutableBytes { raw -> Bool in
            guard let ctx = CGContext(data: raw.baseAddress, width: image.width, height: image.height, bitsPerComponent: 8,
                                      bytesPerRow: image.width * 4, space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
            return true
        }
        guard ok else { return nil }
        data = buffer
    }

    func rgb(_ x: Int, _ y: Int) -> (CGFloat, CGFloat, CGFloat) {
        let i = (y * width + x) * 4
        return (CGFloat(data[i]) / 255, CGFloat(data[i + 1]) / 255, CGFloat(data[i + 2]) / 255)
    }

    private func lum(_ x: Int, _ y: Int) -> Int {
        let i = (y * width + x) * 4
        return (Int(data[i]) * 299 + Int(data[i + 1]) * 587 + Int(data[i + 2]) * 114) / 1000
    }

    /// Grey ink per column (its top and bottom ink row): far from the band's median brightness and
    /// unsaturated (a coloured git bar or icon is not a digit).
    private func inkColumns(_ xs: ClosedRange<Int>, rows: ClosedRange<Int>) -> [Int: ClosedRange<Int>] {
        let xs = xs.clamped(to: 0...(width - 1)), rows = rows.clamped(to: 0...(height - 1))
        var lums: [Int] = []
        for y in rows where y % 2 == 0 {
            for x in xs where x % 2 == 0 { lums.append(lum(x, y)) }
        }
        guard !lums.isEmpty else { return [:] }
        let bg = lums.sorted()[lums.count / 2]
        var out: [Int: ClosedRange<Int>] = [:]
        for x in xs {
            for y in rows {
                let i = (y * width + x) * 4
                let r = Int(data[i]), g = Int(data[i + 1]), b = Int(data[i + 2])
                guard abs(lum(x, y) - bg) > 40, max(r, g, b) - min(r, g, b) < 48 else { continue }
                out[x] = out[x].map { min($0.lowerBound, y)...max($0.upperBound, y) } ?? y...y
            }
        }
        return out
    }

    private func rows(_ y: CGFloat, _ half: CGFloat) -> ClosedRange<Int> {
        Int((y - half).rounded(.down))...max(Int((y - half).rounded(.down)), Int((y + half).rounded(.up)))
    }

    /// Right-most ink column between `from` (right) and `to` (left) on the row at `y`.
    func inkRight(from: CGFloat, to: CGFloat, y: CGFloat, half: CGFloat) -> CGFloat? {
        let lo = Int(to.rounded(.down)), hi = Int(from.rounded(.up))
        guard lo < hi else { return nil }
        return inkColumns(lo...hi, rows: rows(y, half)).keys.max().map { CGFloat($0) }
    }

    /// The number ending near `right` on the row at `y`: its ink, extended left while the gaps
    /// stay under 0.6 glyph, at most 5.5 glyphs wide; 2 px of margin. nil = an empty row.
    func numberCell(right: CGFloat, y: CGFloat, half: CGFloat, glyph: CGFloat) -> CGRect? {
        let lo = Int((right - glyph * 5.5).rounded(.down)), hi = Int((right + glyph * 0.3).rounded(.up))
        guard lo < hi, glyph > 0 else { return nil }
        let ink = inkColumns(lo...hi, rows: rows(y, half))
        guard let last = ink.keys.max(), CGFloat(last) >= right - glyph, var span = ink[last] else { return nil }
        var first = last, gap = 0
        var x = last - 1
        while x >= lo, CGFloat(gap) < glyph * 0.6 {
            if let column = ink[x] {
                first = x; gap = 0
                span = min(span.lowerBound, column.lowerBound)...max(span.upperBound, column.upperBound)
            } else {
                gap += 1
            }
            x -= 1
        }
        // Tight in y too, so the cell's centre is the number's centre (the band fit uses it).
        return CGRect(x: first - 2, y: span.lowerBound - 2, width: last - first + 5, height: span.upperBound - span.lowerBound + 5)
    }
}
#endif
