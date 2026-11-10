import Foundation
import XCTest
@testable import OverlayCore

final class OutageGateTests: XCTestCase {
    func testShortDropIsNotAnOutage() {
        var gate = OutageGate(grace: 30)
        let t0 = Date()
        XCTAssertFalse(gate.failed(at: t0))                       // first failure: retry, stay quiet
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(8)))  // service restarting
        XCTAssertFalse(gate.reached())                            // back: nothing was announced
        XCTAssertNil(gate.since)
    }

    func testAnnouncedOnceAfterGraceThenCleared() {
        var gate = OutageGate(grace: 30)
        let t0 = Date()
        XCTAssertFalse(gate.failed(at: t0))
        XCTAssertTrue(gate.failed(at: t0.addingTimeInterval(31)))
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(45)))  // said only once
        XCTAssertTrue(gate.reached())
        XCTAssertFalse(gate.failed(at: t0.addingTimeInterval(50)))  // a new outage starts its own grace
    }
}
