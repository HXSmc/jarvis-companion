import XCTest
@testable import OverlayCore

final class ListCapTests: XCTestCase {
    func testKeepsNewestInOrder() {
        var items: [Int] = []
        for value in 1...45 {
            items.append(value)
            items.dropOldest(keeping: 40)
        }
        XCTAssertEqual(items.count, 40)
        XCTAssertEqual(items.first, 6)
        XCTAssertEqual(items, Array(6...45))
    }

    func testUnderCapDropsNothing() {
        var items = Array(1...39)
        items.dropOldest(keeping: 40)
        XCTAssertEqual(items, Array(1...39))
    }

    func testAtCapDropsNothing() {
        var items = Array(1...40)
        items.dropOldest(keeping: 40)
        XCTAssertEqual(items, Array(1...40))
    }

    func testEmptyAndZeroLimitDoNotCrash() {
        var empty: [Int] = []
        empty.dropOldest(keeping: 40)
        XCTAssertTrue(empty.isEmpty)
        var items = [1, 2, 3]
        items.dropOldest(keeping: 0)
        XCTAssertEqual(items, [1, 2, 3])
    }
}
