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
        XCTAssertEqual(LineBandFit.fit(ranges: [1...2], digits: gutter(1, 3), window: window, partial: true).reason,
                       "no_gutter")  // a column needs 5 numbers
        XCTAssertEqual(LineBandFit.fit(ranges: [1...2], digits: [], window: window, partial: true).reason, "no_gutter")
        let same = (0..<6).map { (line: 7, centre: CGPoint(x: 40, y: 100 + CGFloat($0) * 20), right: CGFloat(52)) }
        XCTAssertEqual(LineBandFit.fit(ranges: [7...7], digits: same, window: window, partial: true).reason, "no_fit")
        let thin = CGRect(x: 0, y: 80, width: 60, height: 700)
        XCTAssertEqual(LineBandFit.fit(ranges: [3...4], digits: gutter(1, 30), window: thin, partial: true).reason, "narrow")
    }

    func testEditorWindowIsTheFrontMostTitleThatNamesTheFile() {
        let terminal = (title: "ali — -zsh — overlay.pyc", frame: CGRect(x: 0, y: 0, width: 10, height: 10))
        let code = (title: "● overlay.py — jarvis-companion-overlay", frame: CGRect(x: 1, y: 1, width: 10, height: 10))
        let other = (title: "overlay.py — elsewhere", frame: CGRect(x: 2, y: 2, width: 10, height: 10))
        XCTAssertEqual(LineBandFit.editorWindow(file: "gateway/overlay.py", windows: [terminal, code, other]), code.frame)
        XCTAssertNil(LineBandFit.editorWindow(file: "gateway/overlay.py", windows: [terminal]))
        XCTAssertNil(LineBandFit.editorWindow(file: "", windows: [code]))
        XCTAssertNil(LineBandFit.editorWindow(file: "lay.py", windows: [code]))  // a part of a word is no match
    }
}
