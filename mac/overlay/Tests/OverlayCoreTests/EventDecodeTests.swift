import XCTest
@testable import OverlayCore

final class EventDecodeTests: XCTestCase {
    private func decode(_ line: String) -> OverlayEvent {
        OverlayEvent.decode(line: Data(line.utf8))
    }

    func testSay() {
        XCTAssertEqual(decode(#"{"type":"say","text":"Opening Safari."}"#), .say(text: "Opening Safari."))
    }

    func testAudio() {
        let pcm = Data([0x01, 0x00, 0xFF, 0x7F])
        let b64 = pcm.base64EncodedString()
        let event = decode(#"{"type":"audio","text":"On it.","rate":24000,"pcm_b64":"\#(b64)"}"#)
        XCTAssertEqual(event, .audio(text: "On it.", rate: 24000, pcm: pcm))
    }

    func testAudioDefaultsRateWhenMissing() {
        let pcm = Data([9])
        let b64 = pcm.base64EncodedString()
        let event = decode(#"{"type":"audio","text":"hi","pcm_b64":"\#(b64)"}"#)
        XCTAssertEqual(event, .audio(text: "hi", rate: 24000, pcm: pcm))
    }

    func testShapeWithCheck() {
        let event = decode(#"{"type":"shape","shape":{"kind":"rect","box":[10,20,110,80],"label":"Export"},"check":{"role":"AXButton","label":"Export"}}"#)
        let shape = HUDShape(kind: .rect, box: [10, 20, 110, 80], label: "Export")
        XCTAssertEqual(event, .shape(shape, check: AXCheck(role: "AXButton", label: "Export")))
    }

    func testShapeWithoutCheck() {
        let event = decode(#"{"type":"shape","shape":{"kind":"circle","box":[1,2,3,4]}}"#)
        XCTAssertEqual(event, .shape(HUDShape(kind: .circle, box: [1, 2, 3, 4]), check: nil))
    }

    func testCard() {
        let event = decode(#"{"type":"card","card":{"id":"c1","type":"stat","title":"Files","value":"14","label":"since merge"}}"#)
        XCTAssertEqual(event, .card(.stat(StatCard(id: "c1", title: "Files", value: "14", label: "since merge"))))
    }

    func testLines() {
        let event = decode(#"{"type":"lines","file":"/abs/path.py","ranges":[[12,18],[40,41]]}"#)
        XCTAssertEqual(event, .lines(file: "/abs/path.py", ranges: [12...18, 40...41]))
    }

    func testClear() {
        XCTAssertEqual(decode(#"{"type":"clear"}"#), .clear)
    }

    func testDone() {
        let event = decode(#"{"type":"done","metrics":{"stt_final_ms":140,"ack_ms":12.3},"error":null}"#)
        XCTAssertEqual(event, .done(metrics: ["stt_final_ms": 140, "ack_ms": 12.3], error: nil))
    }

    func testDoneWithError() {
        let event = decode(#"{"type":"done","metrics":{},"error":"boom"}"#)
        XCTAssertEqual(event, .done(metrics: [:], error: "boom"))
    }

    func testBadJSONIsUnknown() {
        XCTAssertEqual(decode(#"{"type":"say""#), .unknown)
        XCTAssertEqual(decode("not json at all"), .unknown)
        XCTAssertEqual(decode(#"[]"#), .unknown)
    }

    func testUnknownTypeIsUnknown() {
        XCTAssertEqual(decode(#"{"type":"hologram","text":"x"}"#), .unknown)
        XCTAssertEqual(decode(#"{"text":"no type"}"#), .unknown)
    }

    func testLayoutAside() {
        XCTAssertEqual(decode(#"{"type":"layout","mode":"aside"}"#), .layout(.aside))
    }

    func testLayoutRestore() {
        XCTAssertEqual(decode(#"{"type":"layout","mode":"restore"}"#), .layout(.restore))
    }

    func testLayoutBadModeIsUnknown() {
        XCTAssertEqual(decode(#"{"type":"layout"}"#), .unknown)
        XCTAssertEqual(decode(#"{"type":"layout","mode":"tidy"}"#), .unknown)
        XCTAssertEqual(decode(#"{"type":"layout","mode":1}"#), .unknown)
    }
}
