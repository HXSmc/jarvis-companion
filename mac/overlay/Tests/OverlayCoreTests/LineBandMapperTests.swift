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
}
