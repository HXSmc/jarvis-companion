import XCTest
@testable import OverlayCore

final class LineBandMapperTests: XCTestCase {
    func testPerfectFit() throws {
        let mapper = try XCTUnwrap(LineBandMapper(marks: [(line: 1, midY: 10), (line: 2, midY: 20), (line: 3, midY: 30)]))
        XCTAssertEqual(mapper.a, 10, accuracy: 0.0001)
        XCTAssertEqual(mapper.b, 0, accuracy: 0.0001)
        let band = mapper.band(for: 2...3, x: 100, width: 300)
        XCTAssertEqual(CGFloat(band.minY), 15, accuracy: 0.0001)  // y(2) - h/2 = 20 - 5
        XCTAssertEqual(CGFloat(band.height), 20, accuracy: 0.0001)  // y(3) + 5 - (y(2) - 5)
        XCTAssertEqual(CGFloat(band.minX), 100)
        XCTAssertEqual(CGFloat(band.width), 300)
    }

    func testLeastSquaresFit() throws {
        let mapper = try XCTUnwrap(LineBandMapper(marks: [(line: 1, midY: 10), (line: 2, midY: 23)]))
        XCTAssertEqual(mapper.a, 13, accuracy: 0.0001)
        XCTAssertEqual(mapper.b, -3, accuracy: 0.0001)  // y(1) = 10
    }

    func testNilWhenFewerThanTwoDistinctLines() {
        XCTAssertNil(LineBandMapper(marks: []))
        XCTAssertNil(LineBandMapper(marks: [(line: 5, midY: 40)]))
        XCTAssertNil(LineBandMapper(marks: [(line: 5, midY: 40), (line: 5, midY: 55)]))
    }

    // MARK: LineBandFit (the lines-event pass)

    /// Gutter digits for lines first...last, 20 pt apart, x-centre 40, right edge 52.
    private func gutter(_ first: Int, _ last: Int) -> [(line: Int, centre: CGPoint, right: CGFloat)] {
        (first...last).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 - first) * 20), right: 52) }
    }
    private let window = CGRect(x: 0, y: 80, width: 900, height: 700)

    func testPassBeforeTheScrollIsOffViewNotABandFarOffScreen() {
        // Run 5: the editor still showed lines 1-30 when the 936-949 change was resolved.
        let pass = LineBandFit.fit(ranges: [936...949], digits: gutter(1, 30), window: window, partial: true)
        XCTAssertTrue(pass.bands.isEmpty)
        XCTAssertEqual(pass.reason, "off_view")
        XCTAssertEqual(pass.seen, 1...30)
    }

    func testPassAfterTheScrollDrawsTheChangedLines() throws {
        let pass = LineBandFit.fit(ranges: [936...949, 1339...1340], digits: gutter(926, 958), window: window, partial: false)
        XCTAssertEqual(pass.reason, "ok")
        let band = try XCTUnwrap(pass.bands.first)
        XCTAssertEqual(pass.bands.count, 1)  // 1339-1340 is off view: not extrapolated
        XCTAssertEqual(band.lines, 936...949)
        XCTAssertEqual(band.rect.minY, 100 + 10 * 20 - 10, accuracy: 0.001)  // line 936 centre minus half a line
        XCTAssertEqual(band.rect.maxY, 100 + 23 * 20 + 10, accuracy: 0.001)
        XCTAssertEqual(band.rect.minX, 56, accuracy: 0.001)  // gutter right + 4
    }

    func testPartlyVisibleRangeWaitsUntilTheLastTry() throws {
        let early = LineBandFit.fit(ranges: [20...40], digits: gutter(1, 30), window: window, partial: false)
        XCTAssertEqual(early.reason, "partial")
        XCTAssertTrue(early.bands.isEmpty)
        let last = LineBandFit.fit(ranges: [20...40], digits: gutter(1, 30), window: window, partial: true)
        XCTAssertEqual(last.reason, "ok")
        XCTAssertEqual(try XCTUnwrap(last.bands.first).lines, 20...30)
    }

    func testGuardReasons() {
        XCTAssertEqual(LineBandFit.fit(ranges: [1...2], digits: gutter(1, 1), window: window, partial: true).reason,
                       "no_gutter")  // a 1-line file has no gutter to fit (B4)
        XCTAssertEqual(LineBandFit.fit(ranges: [1...2], digits: [], window: window, partial: true).reason, "no_gutter")
        let same = (0..<6).map { (line: 7, centre: CGPoint(x: 40, y: 100 + CGFloat($0) * 20), right: CGFloat(52)) }
        XCTAssertEqual(LineBandFit.fit(ranges: [7...7], digits: same, window: window, partial: true).reason, "no_fit")
        let thin = CGRect(x: 0, y: 80, width: 60, height: 700)
        XCTAssertEqual(LineBandFit.fit(ranges: [3...4], digits: gutter(1, 30), window: thin, partial: true).reason, "narrow")
    }

    func testEditorWindowIsTheFrontMostTitleThatNamesTheFile() {
        let terminal = (app: "Terminal", title: "ali — -zsh — overlay.pyc", frame: CGRect(x: 0, y: 0, width: 10, height: 10))
        let code = (app: "Code", title: "● overlay.py — jarvis-companion-overlay", frame: CGRect(x: 1, y: 1, width: 10, height: 10))
        let other = (app: "Code", title: "overlay.py — elsewhere", frame: CGRect(x: 2, y: 2, width: 10, height: 10))
        XCTAssertEqual(LineBandFit.editorWindow(file: "gateway/overlay.py", windows: [terminal, code, other]), code.frame)
        XCTAssertNil(LineBandFit.editorWindow(file: "gateway/overlay.py", windows: [terminal]))
        XCTAssertNil(LineBandFit.editorWindow(file: "", windows: [code]))
        XCTAssertNil(LineBandFit.editorWindow(file: "lay.py", windows: [code]))  // a part of a word is no match
    }

    func testEditorWindowNeedsAnEditorApp() {
        // Security note 3e: a browser tab titled like the file must not pick the gutter pass.
        let safari = (app: "Safari", title: "overlay.py", frame: CGRect(x: 0, y: 0, width: 10, height: 10))
        let code = (app: "Code", title: "overlay.py — repo", frame: CGRect(x: 1, y: 1, width: 10, height: 10))
        XCTAssertEqual(LineBandFit.editorWindow(file: "gateway/overlay.py", windows: [safari, code]), code.frame)
        XCTAssertNil(LineBandFit.editorWindow(file: "gateway/overlay.py", windows: [safari]))
    }

    func testThreeHunksGiveThreeBandsInOrder() throws {
        // Gutter for lines 120-170, 20 pt apart from y=100 (gutter(_:_:) above).
        let pass = LineBandFit.fit(ranges: [140...142, 149...149, 156...156], digits: gutter(120, 170),
                                   window: CGRect(x: 0, y: 80, width: 900, height: 1200), partial: false)
        XCTAssertEqual(pass.reason, "ok")
        XCTAssertEqual(pass.bands.map(\.lines), [140...142, 149...149, 156...156])
        let rowTop: (Int) -> CGFloat = { 100 + CGFloat($0 - 120) * 20 - 10 }
        for (band, (first, last)) in zip(pass.bands, [(140, 142), (149, 149), (156, 156)]) {
            XCTAssertEqual(band.rect.minY, rowTop(first), accuracy: 1)
            XCTAssertEqual(band.rect.maxY, rowTop(last) + 20, accuracy: 1)
        }
        XCTAssertEqual(BandRetry.rangeIndex(of: 149...149, in: [140...142, 149...149, 156...156]), 1)
        XCTAssertEqual(BandRetry.rangeIndex(of: 150...151, in: [140...142, 149...160]), 1)  // clipped on the last try
        XCTAssertNil(BandRetry.rangeIndex(of: 1...2, in: [140...142]))
        // R1 design 7: a band over two ranges takes the label of the one it covers most.
        XCTAssertEqual(BandRetry.rangeIndex(of: 141...149, in: [140...142, 145...150]), 1)
        XCTAssertEqual(BandRetry.rangeIndex(of: 140...146, in: [140...142, 145...150]), 0)
        XCTAssertEqual(BandRetry.rangeIndex(of: 141...146, in: [140...142, 145...150]), 0)  // tie: the first
    }

    // MARK: R3 (Mac 33a): VS Code sticky-scroll headers above the scrolling gutter

    /// Gutter rows read off the 33a frame (evidence mac-33/frame-08-14-hud.jpg, 1440 pt wide, y down):
    /// sticky headers 587, 618, 678, 687, 689 on the top rows, 692 half hidden under them, then
    /// 693...732 every 15 pt from y 204. Line 710 truly spans y 452-467.
    private func stickyGutter() -> [(line: Int, centre: CGPoint, right: CGFloat)] {
        let sticky: [(Int, CGFloat)] = [(587, 118), (618, 133), (678, 148), (687, 163), (689, 178), (692, 191)]
        let rows = sticky + (693...732).map { ($0, 204 + CGFloat($0 - 693) * 15) }
        return rows.map { (line: $0.0, centre: CGPoint(x: 215, y: $0.1), right: 225) }
    }

    func testStickyScrollHeadersDoNotShiftTheBand() throws {
        let editor = CGRect(x: 0, y: 35, width: 1440, height: 790)
        let pass = LineBandFit.fit(ranges: [710...710], digits: stickyGutter(), window: editor, partial: false)
        XCTAssertEqual(pass.reason, "ok")
        XCTAssertEqual(pass.seen, 692...732)
        let band = try XCTUnwrap(pass.bands.first?.rect)
        XCTAssertEqual(band.midY, 459.5, accuracy: 1.5)
        XCTAssertEqual(band.height, 15, accuracy: 1)
        let overlap = min(band.maxY, 467) - max(band.minY, 452)
        XCTAssertGreaterThan(overlap / (max(band.maxY, 467) - min(band.minY, 452)), 0.8)  // IoU on y
        // A changed line under a sticky header (689) is not a scrolling line on screen: no band there.
        XCTAssertEqual(LineBandFit.fit(ranges: [689...689], digits: stickyGutter(), window: editor, partial: true).reason,
                       "off_view")
    }

    // MARK: B4/B5: short files, folded ranges, wrapped lines

    func testShortFileGutterGetsABand() throws {
        // B4: 2-4 evenly spaced digits numbered from 1 are a gutter even without a 5-digit column.
        let pass = LineBandFit.fit(ranges: [3...3], digits: gutter(1, 3), window: window, partial: true)
        XCTAssertEqual(pass.reason, "ok")
        let band = try XCTUnwrap(pass.bands.first)
        XCTAssertEqual(band.lines, 3...3)
        XCTAssertEqual(band.rect.midY, 100 + 2 * 20, accuracy: 2)  // line 3's centre row
        XCTAssertEqual(LineBandFit.fit(ranges: [1...1], digits: gutter(1, 1), window: window, partial: true).reason,
                       "no_gutter")  // a single digit is not a gutter
        XCTAssertEqual(LineBandFit.fit(ranges: [2...2], digits: gutter(2, 4), window: window, partial: true).reason,
                       "no_gutter")  // not numbered from 1
    }

    func testShortFileGutterSkipsStrayDigitsLeftOfIt() throws {
        // Mac run 2, B4: a 3-line file (gutter 1-4 with the trailing newline) said no_gutter on every
        // try: VS Code's account badge "1" and the status bar's "0" sit left of the gutter column.
        let strays: [(line: Int, centre: CGPoint, right: CGFloat)] = [
            (line: 1, centre: CGPoint(x: 4, y: 700), right: 8), (line: 0, centre: CGPoint(x: 18, y: 770), right: 22)]
        let pass = LineBandFit.fit(ranges: [3...3], digits: strays + gutter(1, 4), window: window, partial: false)
        XCTAssertEqual(pass.reason, "ok")
        let band = try XCTUnwrap(pass.bands.first)
        XCTAssertEqual(band.rect.midY, 100 + 2 * 20, accuracy: 2)  // line 3's row
        XCTAssertEqual(band.rect.minX, 56, accuracy: 0.001)  // right of the real gutter, not the badge
        XCTAssertEqual(LineBandFit.fit(ranges: [1...1], digits: strays + gutter(1, 1), window: window,
                                       partial: true).reason, "no_gutter")  // still no 1-line gutter
    }

    func testSameBandsComparesTheBoxes() {
        let early = LineBandFit.fit(ranges: [12...12], digits: gutter(1, 30), window: window, partial: false)
        let shifted = gutter(1, 30).map { (line: $0.line, centre: CGPoint(x: 40, y: $0.centre.y - 9 * 20), right: $0.right) }
        let late = LineBandFit.fit(ranges: [12...12], digits: shifted, window: window, partial: false)
        XCTAssertEqual(early.reason, "ok")
        XCTAssertEqual(late.reason, "ok")
        XCTAssertTrue(LineBandFit.sameBands(early, early))
        XCTAssertFalse(LineBandFit.sameBands(early, late))  // the editor scrolled 9 rows in between
        let edgeMiss = LineBandFit.fit(ranges: [12...12], digits: Array(gutter(1, 30).dropFirst()), window: window,
                                       partial: false)  // OCR missed line 1: `seen` moves, the box does not
        XCTAssertNotEqual(edgeMiss.seen, early.seen)
        XCTAssertTrue(LineBandFit.sameBands(early, edgeMiss))
    }

    func testBandRetryDrawsOnlyASettledPass() async {
        // Mac run 2, B5: try 1 read line 149 where it sat before `code -g` centred it (y 627) and the
        // box stayed there; try 2 read it after the scroll (y 465). 0 = a pass with no bands.
        func run(_ ys: [Int], tries: Int = 3) async -> (pass: Int, attempt: Int)? {
            await BandRetry.run(tries: tries, pause: { _ in }, pass: { ys[$0 - 1] }, done: { $0 > 0 },
                                same: { $0 == $1 })
        }
        var result = await run([627, 465, 465])
        XCTAssertEqual(result?.pass, 465)
        XCTAssertEqual(result?.attempt, 3)
        result = await run([465, 465, 999])
        XCTAssertEqual(result?.attempt, 2)  // two passes agree: drawn, no third pass
        result = await run([627, 465], tries: 2)
        XCTAssertEqual(result?.pass, 465)  // the last try is taken as it is
        result = await run([0, 0, 465])
        XCTAssertEqual(result?.pass, 465)
        result = await run([627, 0, 0])
        XCTAssertEqual(result?.pass, 0)  // the early pass was never confirmed: nothing drawn
        let plain = await BandRetry.run(tries: 3, pause: { _ in }, pass: { [627, 465, 465][$0 - 1] }, done: { $0 > 0 })
        XCTAssertEqual(plain?.attempt, 1)  // without `same`: the first good pass, as before
    }

    func testFoldedRangeIsNotBoxed() {
        // B5a: the editor folded lines 10-29 away, so line 30 sits one row below line 9.
        let folded = (1...9).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 - 1) * 20), right: CGFloat(52)) }
            + (30...40).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 - 21) * 20), right: CGFloat(52)) }
        let pass = LineBandFit.fit(ranges: [12...12], digits: folded, window: window, partial: true)
        XCTAssertEqual(pass.reason, "folded")
        XCTAssertTrue(pass.bands.isEmpty)
    }

    func testWrappedLineBandCoversItsRows() throws {
        // B5b: line 5 wraps over 3 rows (line 6 starts 3 rows below line 5's centre). Lines 6-9
        // only after the wrap: with 6-11 evenRun would pick that longer run and the pass would
        // say off_view for line 5.
        let wrapped = (1...5).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 - 1) * 20), right: CGFloat(52)) }
            + (6...9).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 + 1) * 20), right: CGFloat(52)) }
        let pass = LineBandFit.fit(ranges: [5...5], digits: wrapped, window: window, partial: true)
        XCTAssertEqual(pass.reason, "ok")
        let band = try XCTUnwrap(pass.bands.first)
        XCTAssertEqual(band.lines, 5...5)
        XCTAssertGreaterThanOrEqual(band.rect.height, 2.4 * 20)  // the wrapped rows, not one line
        XCTAssertEqual(band.rect.minY, 100 + 4 * 20 - 10, accuracy: 0.5)  // line 5 centre minus half a row
    }

    func testEvenRunSkipsOutliersAndKeepsAFlatGutter() {
        let flat = (120...170).map { (line: $0, midY: 100 + CGFloat($0 - 120) * 20) }
        XCTAssertEqual(LineBandMapper.evenRun(flat).map(\.line), Array(120...170))
        // One misread number in the middle (a 7 read as 1: 147 -> 141) is dropped, the rest kept.
        var misread = flat
        misread[27] = (line: 141, midY: misread[27].midY)
        XCTAssertEqual(LineBandMapper.evenRun(misread).map(\.line), Array(120...170).filter { $0 != 147 })
        // Missing rows (OCR skipped some) keep the grid: 120, 123, 124, 130 on a 20 pt pitch.
        let gaps = [120, 123, 124, 130].map { (line: $0, midY: 100 + CGFloat($0 - 120) * 20) }
        XCTAssertEqual(LineBandMapper.evenRun(gaps).map(\.line), [120, 123, 124, 130])
        // Line numbers growing upward (negative pitch) work the same.
        let up: [(line: Int, midY: CGFloat)] = (1...8).map { (line: $0, midY: 500 - CGFloat($0) * 18) } + [(line: 40, midY: 482)]
        XCTAssertEqual(LineBandMapper.evenRun(up).map(\.line), Array(1...8))
        XCTAssertEqual(LineBandMapper.evenRun([(line: 5, midY: 40)]).map(\.line), [5])
    }
}

#if canImport(Vision)
import CoreText

/// Mac run 3 (B4, B5 fold): Vision never read a 1-digit gutter number, so `seen` started at 10.
/// These render a VS Code-like editor at 2x (Menlo 12 pt, 18 pt rows, grey numbers right-aligned,
/// a blue git bar and a blue icon on the changed row) and run the real reader on it.
final class GutterOCRTests: XCTestCase {
    /// Row n's text centre in pixels (baseline 250 + 36 n from the top, cap height about 17 px).
    static func rowY(_ n: Int) -> CGFloat { CGFloat(250 + 36 * n) - 8 }

    static func render(lines: Int, from first: Int = 1, code: (Int) -> String, changed: Set<Int>) -> CGImage {
        let width = 2880, height = 1800
        let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(CGColor(red: 0.12, green: 0.12, blue: 0.12, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let font = CTFontCreateWithName("Menlo" as CFString, 24, nil)
        func draw(_ text: String, x: CGFloat, top: CGFloat, grey: CGFloat, right: Bool = false) {
            let attrs: [NSAttributedString.Key: Any] = [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(red: grey, green: grey, blue: grey, alpha: 1)]
            let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attrs))
            let w = CTLineGetTypographicBounds(line, nil, nil, nil)
            ctx.textPosition = CGPoint(x: right ? x - CGFloat(w) : x, y: CGFloat(height) - top)
            CTLineDraw(line, ctx)
        }
        for (i, item) in ["EXPLORER", "> .vscode", "imports.ts", "sample.py", "side.txt"].enumerated() {
            draw(item, x: 120, top: 160 + CGFloat(i) * 44, grey: 0.8)
        }
        for n in 1...lines {
            let top = CGFloat(250 + 36 * n)
            draw("\(first + n - 1)", x: 800, top: top, grey: changed.contains(n) ? 0.85 : 0.45, right: true)
            draw(code(first + n - 1), x: 840, top: top, grey: 0.8)
            if changed.contains(n) {
                ctx.setFillColor(CGColor(red: 0.2, green: 0.45, blue: 0.95, alpha: 1))  // CTLineDraw changes the fill colour
                ctx.fill(CGRect(x: 812, y: CGFloat(height) - top - 8, width: 6, height: 36))            // git bar
                ctx.fillEllipse(in: CGRect(x: 732, y: CGFloat(height) - top - 2, width: 20, height: 20))  // gutter icon
            }
        }
        return ctx.makeImage()!
    }

    /// Reader output in points (scale 2), as ScreenContext.lineBands feeds LineBandFit.
    static func points(_ found: [(line: Int, box: [Double])]) -> [(line: Int, centre: CGPoint, right: CGFloat)] {
        found.map { (line: $0.line, centre: CGPoint(x: ($0.box[0] + $0.box[2]) / 4, y: ($0.box[1] + $0.box[3]) / 4),
                     right: CGFloat($0.box[2] / 2)) }
    }

    static let window = CGRect(x: 0, y: 0, width: 2880, height: 1800)

    func testDoubledText() {
        XCTAssertEqual(GutterOCR.doubled("66"), 6)
        XCTAssertEqual(GutterOCR.doubled("8 8"), 8)
        XCTAssertEqual(GutterOCR.doubled("1010"), 10)
        XCTAssertNil(GutterOCR.doubled("61"))
        XCTAssertNil(GutterOCR.doubled("6"))
        XCTAssertNil(GutterOCR.doubled("||"))
        XCTAssertNil(GutterOCR.doubled(""))
        XCTAssertNil(GutterOCR.doubled("123456123456"))
    }

    func testOneDigitLinesAreRead() throws {
        let image = Self.render(lines: 37, code: { $0 == 13 ? "" : "import { a\($0) } from \"./a\($0)\";" }, changed: [6, 30])
        let pass1 = GutterOCR.text(in: image)
        // The cause, pinned: pass 1 alone reads no 1-digit number, and line 6 is off view.
        XCTAssertTrue(pass1.compactMap { ShapeTrack.lineNumber($0.text) }.allSatisfy { $0 >= 10 })
        let before = LineBandFit.fit(ranges: [6...6], digits: Self.points(pass1.compactMap { o in
            ShapeTrack.lineNumber(o.text).map { (line: $0, box: o.box) } }), window: CGRect(x: 0, y: 0, width: 1440, height: 900), partial: true)
        XCTAssertNotEqual(before.reason, "ok")

        let found = GutterOCR.lineDigits(in: image, observations: pass1, window: Self.window)
        let lines = Set(found.map(\.line))
        XCTAssertTrue(Set(1...9).isSubset(of: lines), "read \(found.map(\.line).sorted()) pass1 \(pass1.map { "\($0.text)@\(Int($0.box[0]))" })")
        for hit in found where hit.line <= 9 {
            XCTAssertEqual(CGFloat((hit.box[1] + hit.box[3]) / 2), Self.rowY(hit.line), accuracy: 6, "line \(hit.line)")
        }
        let pass = LineBandFit.fit(ranges: [6...6], digits: Self.points(found),
                                   window: CGRect(x: 0, y: 0, width: 1440, height: 900), partial: true)
        XCTAssertEqual(pass.reason, "ok")
        XCTAssertEqual(pass.seen?.lowerBound, 1)
        let band = try XCTUnwrap(pass.bands.first?.rect)
        XCTAssertEqual(band.midY, Self.rowY(6) / 2, accuracy: 3)
    }

    func testShortFileGutterIsRead() throws {
        let words = ["one", "two", "three changed", ""]
        let image = Self.render(lines: 4, code: { words[$0 - 1] }, changed: [3])
        let pass1 = GutterOCR.text(in: image)
        XCTAssertTrue(pass1.compactMap { ShapeTrack.lineNumber($0.text) }.isEmpty)
        let found = GutterOCR.lineDigits(in: image, observations: pass1, window: Self.window)
        XCTAssertEqual(Set(found.map(\.line)), [1, 2, 3], "line 4 has no text to anchor its row; pass1 \(pass1.map { "\($0.text)@\(Int($0.box[0]))" })")
        let pass = LineBandFit.fit(ranges: [3...3], digits: Self.points(found),
                                   window: CGRect(x: 0, y: 0, width: 1440, height: 900), partial: false)
        XCTAssertEqual(pass.reason, "ok")
        let band = try XCTUnwrap(pass.bands.first?.rect)
        XCTAssertEqual(band.midY, Self.rowY(3) / 2, accuracy: 3)
    }

    func testScrolledFileSkipsTheSecondRead() {
        // Lines 121-150 on screen: no 1-digit line can be above them, so no extra OCR pass.
        let image = Self.render(lines: 30, from: 121, code: { "let v\($0) = \($0);" }, changed: [])
        XCTAssertEqual(GutterOCR.cellsToRead(in: image, observations: GutterOCR.text(in: image), window: Self.window), [])
    }
}
#endif
