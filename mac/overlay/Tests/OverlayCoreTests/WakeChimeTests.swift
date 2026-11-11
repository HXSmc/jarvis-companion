import Foundation
import XCTest
@testable import OverlayCore

final class WakeChimeTests: XCTestCase {
    private func name(_ value: String?) -> String? {
        WakeChime.soundName(env: value.map { [WakeChime.envKey: $0] } ?? [:])
    }

    func testUnsetAndEmptyUseTink() {
        XCTAssertEqual(WakeChime.soundName(env: [:]), "Tink")
        XCTAssertEqual(name(""), "Tink")
    }

    func testOffValuesDisableInAnyCase() {
        for value in ["0", "off", "OFF", "none", "false", "False"] {
            XCTAssertNil(name(value), value)
        }
    }

    func testValidNameIsUsed() {
        XCTAssertEqual(name("Glass"), "Glass")
    }

    func testUnsafeOrOversizedNamesFallBackToTink() {
        XCTAssertEqual(name("../x"), "Tink")
        XCTAssertEqual(name("/System/Library/Sounds/Tink.aiff"), "Tink")
        XCTAssertEqual(name(String(repeating: "a", count: 33)), "Tink")
        XCTAssertEqual(name("Glass.aiff"), "Tink")
        XCTAssertEqual(name("a/b"), "Tink")
    }

    func testMaxLengthNameIsUsed() {
        let long = String(repeating: "a", count: 32)
        XCTAssertEqual(name(long), long)
    }
}
