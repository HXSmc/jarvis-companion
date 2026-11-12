import CoreAudio
import XCTest
@testable import OverlayCore

final class AudioRouteTests: XCTestCase {
    private let hdpn = UInt32(0x6864706E) // 'hdpn'
    private let ispk = UInt32(0x6973706B) // 'ispk'

    func testBluetoothIsHeadphones() {
        XCTAssertTrue(AudioRoute.isHeadphones(transport: UInt32(kAudioDeviceTransportTypeBluetooth), dataSource: nil))
    }

    func testBluetoothLEIsHeadphones() {
        XCTAssertTrue(AudioRoute.isHeadphones(transport: UInt32(kAudioDeviceTransportTypeBluetoothLE), dataSource: ispk))
    }

    func testBuiltInHeadphoneJackIsHeadphones() {
        XCTAssertTrue(AudioRoute.isHeadphones(transport: UInt32(kAudioDeviceTransportTypeBuiltIn), dataSource: hdpn))
    }

    func testBuiltInSpeakerIsNotHeadphones() {
        XCTAssertFalse(AudioRoute.isHeadphones(transport: kAudioDeviceTransportTypeBuiltIn, dataSource: ispk))
    }

    func testBuiltInWithoutDataSourceIsNotHeadphones() {
        XCTAssertFalse(AudioRoute.isHeadphones(transport: kAudioDeviceTransportTypeBuiltIn, dataSource: nil))
    }

    func testOtherTransportsFailSafe() {
        let cases: [(UInt32, UInt32?)] = [
            (UInt32(kAudioDeviceTransportTypeHDMI), hdpn),
            (UInt32(kAudioDeviceTransportTypeDisplayPort), hdpn),
            (UInt32(kAudioDeviceTransportTypeAirPlay), hdpn),
            (UInt32(kAudioDeviceTransportTypeUSB), hdpn),
            (0, hdpn),
        ]
        for (transport, dataSource) in cases {
            XCTAssertFalse(
                AudioRoute.isHeadphones(transport: transport, dataSource: dataSource),
                "transport 0x\(String(transport, radix: 16))"
            )
        }
    }
}

final class BargeModeTests: XCTestCase {
    func testValidModeStringWins() {
        XCTAssertEqual(BargeMode.from(health: "off", bargeIn: true, bargeAec: true), .off)
        XCTAssertEqual(BargeMode.from(health: "headphones", bargeIn: false, bargeAec: true), .headphones)
        XCTAssertEqual(BargeMode.from(health: "aec", bargeIn: false, bargeAec: false), .aec)
    }

    func testInvalidModeStringFallsThrough() {
        XCTAssertEqual(BargeMode.from(health: "loud", bargeIn: true, bargeAec: false), .headphones)
        XCTAssertEqual(BargeMode.from(health: "loud", bargeIn: nil, bargeAec: true), .aec)
    }

    func testAecNeedsBargeAecTrueAndBargeInNotFalse() {
        XCTAssertEqual(BargeMode.from(health: nil, bargeIn: true, bargeAec: true), .aec)
        XCTAssertEqual(BargeMode.from(health: nil, bargeIn: nil, bargeAec: true), .aec)
        XCTAssertEqual(BargeMode.from(health: nil, bargeIn: false, bargeAec: true), .off)
    }

    func testPlainBargeInIsHeadphones() {
        XCTAssertEqual(BargeMode.from(health: nil, bargeIn: true, bargeAec: false), .headphones)
        XCTAssertEqual(BargeMode.from(health: nil, bargeIn: true, bargeAec: nil), .headphones)
    }

    func testDefaultsToOff() {
        XCTAssertEqual(BargeMode.from(health: nil, bargeIn: nil, bargeAec: nil), .off)
        XCTAssertEqual(BargeMode.from(health: nil, bargeIn: false, bargeAec: nil), .off)
    }
}
