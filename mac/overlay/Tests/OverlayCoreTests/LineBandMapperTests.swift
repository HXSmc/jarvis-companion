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

    func testFoldedRangeIsNotBoxed() {
        // B5a: the editor folded lines 10-29 away, so line 30 sits one row below line 9.
        let folded = (1...9).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 - 1) * 20), right: 52) }
            + (30...40).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 - 21) * 20), right: 52) }
        let pass = LineBandFit.fit(ranges: [12...12], digits: folded, window: window, partial: true)
        XCTAssertEqual(pass.reason, "folded")
        XCTAssertTrue(pass.bands.isEmpty)
    }

    func testWrappedLineBandCoversItsRows() throws {
        // B5b: line 5 wraps over 3 rows (line 6 starts 3 rows below line 5's centre). Lines 6-9
        // only after the wrap: with 6-11 evenRun would pick that longer run and the pass would
        // say off_view for line 5.
        let wrapped = (1...5).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 - 1) * 20), right: 52) }
            + (6...9).map { (line: $0, centre: CGPoint(x: 40, y: 100 + CGFloat($0 + 1) * 20), right: 52) }
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
