import XCTest
@testable import OverlayCore

final class ServicePushTests: XCTestCase {
    func testSSELinesPairEachEventWithItsData() {
        var sse = SSELines()
        XCTAssertNil(sse.feed("event: hello"))
        XCTAssertEqual(sse.feed("data: {}")?.name, "hello")
        XCTAssertNil(sse.feed(""))
        XCTAssertNil(sse.feed(": ping"))
        XCTAssertNil(sse.feed("data: {}"))  // data with no event name before it
        XCTAssertNil(sse.feed("event: hud"))
        let got = sse.feed(#"data: {"type":"say","text":"x"}"#)
        XCTAssertEqual(got?.name, "hud")
        XCTAssertEqual(got?.data, #"{"type":"say","text":"x"}"#)
        XCTAssertNil(sse.feed("event: wake"))
        XCTAssertEqual(sse.feed(#"data: {"source":"mac"}"#)?.name, "wake")
    }

    /// The exact events gateway/overlay.py `_confirm_watchdog` pushes when a Confirm expires.
    func testExpiredConfirmReplacesTheCardAndSpeaksOnlyWhenIdle() {
        var sse = SSELines()
        _ = sse.feed("event: hud")
        let data = sse.feed(#"data: {"type": "card", "card": {"id": "agentconfirm", "type": "info", "title": "Not done: it timed out", "body": ["run git push in repo"]}}"#)!.data
        let card = OverlayEvent.decode(line: Data(data.utf8))
        let expected = HUDCard.info(InfoCard(id: "agentconfirm", title: "Not done: it timed out",
                                             body: ["run git push in repo"]))
        XCTAssertEqual(card, OverlayEvent.card(expected))
        XCTAssertEqual(card.pushed(idle: false), OverlayEvent.card(expected))

        let line = OverlayEvent.decode(line: Data(#"{"type": "say", "text": "That request expired, so I didn't do it."}"#.utf8))
        XCTAssertEqual(line.pushed(idle: true), line)
        XCTAssertNil(line.pushed(idle: false))
        let audio = OverlayEvent.audio(text: "x", rate: 24000, pcm: Data([1, 0]))
        XCTAssertNil(audio.pushed(idle: false))
        XCTAssertEqual(audio.pushed(idle: true), audio)
    }

    func testOtherPushedEventsAreIgnored() {
        XCTAssertNil(OverlayEvent.clear.pushed(idle: true))
        XCTAssertNil(OverlayEvent.lines(file: "/x", ranges: [1...2]).pushed(idle: true))
        XCTAssertNil(OverlayEvent.done(metrics: [:], error: nil).pushed(idle: true))
        XCTAssertNil(OverlayEvent.unknown.pushed(idle: true))
    }
}
