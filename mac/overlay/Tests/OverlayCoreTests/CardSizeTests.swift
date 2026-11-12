import CoreGraphics
import XCTest
@testable import OverlayCore

final class CardSizeTests: XCTestCase {
    /// 360x240 into 1440x900: capped by height (720), keeps its 1.5 aspect, centred.
    func testEnlargedFrameCentredAndCapped() {
        let frame = CardSize.enlargedFrame(natural: CGSize(width: 360, height: 240),
                                           visible: CGRect(x: 0, y: 0, width: 1440, height: 900))
        XCTAssertEqual(frame.midX, 720, accuracy: 0.5)
        XCTAssertEqual(frame.midY, 450, accuracy: 0.5)
        XCTAssertEqual(frame.width / frame.height, 1.5, accuracy: 1.5 * 0.005)
        XCTAssertLessThanOrEqual(frame.width, 1152.001)
        XCTAssertLessThanOrEqual(frame.height, 720.001)
        XCTAssertTrue(min(abs(frame.width - 1152), abs(frame.height - 720)) <= 0.5,
                      "one dimension must sit at its cap")
        let offset = CardSize.enlargedFrame(natural: CGSize(width: 360, height: 240),
                                            visible: CGRect(x: 100, y: 50, width: 1440, height: 900))
        XCTAssertEqual(offset.midX, 100 + 720, accuracy: 0.5)
        XCTAssertEqual(offset.midY, 50 + 450, accuracy: 0.5)
    }

    func testEnlargeState() {
        var state = EnlargeState()
        XCTAssertNil(state.max("a"))
        XCTAssertEqual(state.max("b"), "a")
        XCTAssertEqual(state.id, "b")
        state.restore()
        XCTAssertNil(state.id)
        XCTAssertNil(state.max("a"))
        state.dropped(ids: ["a"])
        XCTAssertNil(state.id)
        XCTAssertNil(state.max("q"))
        state.dropped(ids: ["z"])
        XCTAssertEqual(state.id, "q")
    }
}
