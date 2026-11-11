import XCTest
@testable import OverlayCore

/// f52-cardfix. Top-left points, y down.
final class CardFixTests: XCTestCase {

    // MARK: D5 one duration per group move

    func testGroupMoveDurationUsesLargestDistance() {
        let old = [CGRect(x: 0, y: 0, width: 100, height: 100), CGRect(x: 500, y: 0, width: 100, height: 100)]
        let new = [CGRect(x: 1080, y: 0, width: 100, height: 100), CGRect(x: 600, y: 0, width: 100, height: 100)]
        XCTAssertEqual(ScatterLayout.groupMoveDuration(old: old, new: new, across: 1440),
                       ScatterLayout.moveDuration(distance: 1080, across: 1440), accuracy: 0.0001)
        XCTAssertEqual(ScatterLayout.groupMoveDuration(old: old, new: new, across: 1440), 0.3375, accuracy: 0.0001)
        XCTAssertEqual(ScatterLayout.groupMoveDuration(old: [], new: [], across: 1440), ScatterLayout.motionShort)
    }

    // MARK: D3 named notification zone

    func testNotificationZoneUsesTheNamedSize() {
        let visible = CGRect(x: 0, y: 25, width: 1440, height: 875)
        XCTAssertEqual(ScatterLayout.notificationZone(visible: visible).size, ScatterLayout.notificationZoneSize)
        XCTAssertEqual(ScatterLayout.notificationZone(visible: visible).maxX, visible.maxX)
        XCTAssertEqual(ScatterLayout.notificationZone(visible: visible).minY, visible.minY)
    }
}
