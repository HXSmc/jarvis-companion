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

    func testLinesWithLabels() {
        let event = decode(#"{"type":"lines","file":"/a.py","ranges":[[140,142],[149,149],[155,155]],"labels":["1/3 +3","2/3 changed","3/3 removed here"]}"#)
        XCTAssertEqual(event, .lines(file: "/a.py", ranges: [140...142, 149...149, 155...155],
                                     labels: ["1/3 +3", "2/3 changed", "3/3 removed here"]))
        // A dropped (inverted) pair drops its own label, the others stay paired.
        let dropped = decode(#"{"type":"lines","file":"/a.py","ranges":[[9,3],[149,149]],"labels":["1/2 changed","2/2 changed"]}"#)
        XCTAssertEqual(dropped, .lines(file: "/a.py", ranges: [149...149], labels: ["2/2 changed"]))
        // Long labels are clipped to 24 characters.
        let long = decode(#"{"type":"lines","file":"/a.py","ranges":[[1,1]],"labels":["\#(String(repeating: "x", count: 40))"]}"#)
        XCTAssertEqual(long, .lines(file: "/a.py", ranges: [1...1], labels: [String(repeating: "x", count: 24)]))
    }

    func testLinesWithoutLabelsKeepsChangedLabel() {
        // No labels (an older service) or a list of the wrong length: no labels, the app draws "changed".
        XCTAssertEqual(decode(#"{"type":"lines","file":"/a.py","ranges":[[1,2]]}"#),
                       .lines(file: "/a.py", ranges: [1...2], labels: []))
        XCTAssertEqual(decode(#"{"type":"lines","file":"/a.py","ranges":[[1,2],[5,5]],"labels":["1/2 changed"]}"#),
                       .lines(file: "/a.py", ranges: [1...2, 5...5], labels: []))
        XCTAssertEqual(decode(#"{"type":"lines","file":"/a.py","ranges":[[1,2]],"labels":[7]}"#),
                       .lines(file: "/a.py", ranges: [1...2], labels: []))
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

    func testDropCards() {
        XCTAssertEqual(decode(#"{"type":"drop_cards","ids":["gd-stat","x"]}"#), .dropCards(ids: ["gd-stat", "x"]))
        let long = String(repeating: "a", count: 65)
        XCTAssertEqual(decode(#"{"type":"drop_cards","ids":["a",3,null,"\#(long)","b"]}"#), .dropCards(ids: ["a", "b"]))
        let many = (0..<30).map { "\"c\($0)\"" }.joined(separator: ",")
        XCTAssertEqual(decode(#"{"type":"drop_cards","ids":[\#(many)]}"#), .dropCards(ids: (0..<24).map { "c\($0)" }))
        XCTAssertEqual(decode(#"{"type":"drop_cards"}"#), .unknown)
        XCTAssertEqual(decode(#"{"type":"drop_cards","ids":"gd-stat"}"#), .unknown)
        // Pushed outside a turn it is ignored: only turn streams replace cards.
        XCTAssertNil(OverlayEvent.dropCards(ids: ["gd-stat"]).pushed(idle: true))
    }

    func testEndListeningAndCardSize() {
        XCTAssertEqual(ControlEvent.decode(line: Data(#"{"type":"end_listening"}"#.utf8)), .endListening)
        XCTAssertEqual(ControlEvent.decode(line: Data(#"{"type":"card_size","id":"ab12","size":"max"}"#.utf8)),
                       .cardSize(id: "ab12", max: true))
        XCTAssertEqual(ControlEvent.decode(line: Data(#"{"type":"card_size","id":"ab12","size":"restore"}"#.utf8)),
                       .cardSize(id: "ab12", max: false))
        // "huge" is not a size: no control event, and OverlayEvent alone still sees .unknown.
        let huge = #"{"type":"card_size","id":"ab12","size":"huge"}"#
        XCTAssertNil(ControlEvent.decode(line: Data(huge.utf8)))
        XCTAssertEqual(decode(huge), .unknown)
        XCTAssertEqual(TurnEvent.decode(line: Data(#"{"type":"clear"}"#.utf8)), .overlay(.clear))
        XCTAssertEqual(TurnEvent.decode(line: Data(#"{"type":"end_listening"}"#.utf8)),
                       .control(.endListening))
    }
}
