import XCTest
@testable import OverlayCore

final class FixtureDecodeTests: XCTestCase {
    // .../mac/overlay/Tests/OverlayCoreTests/FixtureDecodeTests.swift -> repo root
    static let fixturesDir = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()  // OverlayCoreTests
        .deletingLastPathComponent()  // Tests
        .deletingLastPathComponent()  // overlay
        .deletingLastPathComponent()  // mac
        .deletingLastPathComponent()  // repo root
        .appendingPathComponent("tests")
        .appendingPathComponent("fixtures")
        .appendingPathComponent("hud")

    private func fixtureFiles() throws -> [URL] {
        let files = try FileManager.default.contentsOfDirectory(at: Self.fixturesDir, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" && $0.lastPathComponent != "schema.json" }
        XCTAssertFalse(files.isEmpty, "no fixtures in \(Self.fixturesDir.path)")
        return files
    }

    func testEveryFixtureDecodesWithExactCounts() throws {
        var kinds = Set<HUDShape.Kind>()
        var types = Set<String>()
        for file in try fixtureFiles() {
            let data = try Data(contentsOf: file)
            let msg = try JSONDecoder().decode(HUDMessage.self, from: data)
            let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
            let rawShapes = raw["shapes"] as? [[String: Any]] ?? []
            let rawCards = raw["cards"] as? [[String: Any]] ?? []
            XCTAssertEqual(msg.shapes.count, rawShapes.count, file.lastPathComponent)
            XCTAssertEqual(msg.cards.count, rawCards.count, file.lastPathComponent)
            kinds.formUnion(msg.shapes.map(\.kind))
            types.formUnion(msg.cards.map(\.typeName))
        }
        XCTAssertEqual(kinds, Set<HUDShape.Kind>([.rect, .circle, .triangle]))
        XCTAssertEqual(types, Set(["info", "stat", "bar", "flow", "link", "image", "video"]))
    }

    func testLossyArraysSkipUnknownKindAndType() throws {
        let json = #"{"shapes":[{"kind":"arrow","box":[1,2,3,4]},{"kind":"rect","box":[10,20,30,40]}],"cards":[{"id":"x1","type":"hologram","title":"Nope"},{"id":"x2","type":"info","title":"Yes","body":["kept"]}]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
        XCTAssertEqual(msg.shapes.count, 1)
        XCTAssertEqual(msg.shapes.first?.kind, .rect)
        XCTAssertEqual(msg.shapes.first?.rect, CGRect(x: 10, y: 20, width: 20, height: 20))
        XCTAssertEqual(msg.cards.count, 1)
        guard case .info(let card) = msg.cards[0] else {
            return XCTFail("expected info card, got \(msg.cards)")
        }
        XCTAssertEqual(card.title, "Yes")
        XCTAssertEqual(card.body, ["kept"])
    }

    func testShapeBoxMustHaveFourNumbers() throws {
        let json = #"{"shapes":[{"kind":"rect","box":[1,2,3]}],"cards":[]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
        XCTAssertTrue(msg.shapes.isEmpty, "3-number box must be dropped")
    }

    func testRoundTripEncodesBackToWireFormat() throws {
        let data = try Data(contentsOf: Self.fixturesDir.appendingPathComponent("flow.json"))
        let msg = try JSONDecoder().decode(HUDMessage.self, from: data)
        let out = try JSONEncoder().encode(msg)
        let back = try JSONDecoder().decode(HUDMessage.self, from: out)
        XCTAssertEqual(msg, back)
        guard case .flow(let card) = back.cards[0] else { return XCTFail("flow fixture card missing") }
        XCTAssertEqual(card.edges.first, FlowEdge(from: "a", to: "b", label: nil))
        XCTAssertEqual(
            card.edges.last,
            FlowEdge(from: "c", to: "d", label: "shapes + cards")
        )
    }

    func testLayoutEventFixtureDecodes() throws {
        let url = Self.fixturesDir.deletingLastPathComponent()
            .appendingPathComponent("overlay")
            .appendingPathComponent("events")
            .appendingPathComponent("layout.ndjson")
        let text = try String(contentsOf: url, encoding: .utf8)
        let events = text.components(separatedBy: "\n")
            .filter { !$0.isEmpty }
            .map { OverlayEvent.decode(line: Data($0.utf8)) }
        XCTAssertEqual(events, [.layout(.aside), .layout(.restore), .clear])
    }

    // MARK: Media fixtures and guards

    func testVideoFixtureDecodes() throws {
        let msg = try JSONDecoder().decode(HUDMessage.self, from: try Data(contentsOf: Self.fixturesDir.appendingPathComponent("video.json")))
        XCTAssertEqual(msg.cards.count, 2)
        guard case .video(let yt) = msg.cards[0] else { return XCTFail("expected a video card, got \(msg.cards)") }
        XCTAssertEqual(yt.youtubeID, "aqz-KE-bpKQ")
        XCTAssertNil(yt.url)
        guard case .video(let file) = msg.cards[1] else { return XCTFail("expected a video card, got \(msg.cards)") }
        XCTAssertNotNil(file.url)
        XCTAssertNotNil(file.thumbnailURL)
    }

    func testGalleryFixtureDecodesThreeItems() throws {
        let msg = try JSONDecoder().decode(HUDMessage.self, from: try Data(contentsOf: Self.fixturesDir.appendingPathComponent("gallery.json")))
        XCTAssertEqual(msg.cards.count, 1)
        guard case .image(let card) = msg.cards[0] else { return XCTFail("expected an image card, got \(msg.cards)") }
        XCTAssertEqual(card.images.count, 3)
    }

    func testBadYouTubeIDCardIsSkipped() throws {
        let json = #"{"shapes":[],"cards":[{"id":"v1","type":"video","title":"Bad","youtube_id":"bad"}]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
        XCTAssertTrue(msg.cards.isEmpty, "a card with a malformed youtube_id must be dropped")
    }

    func testGalleryDropsUnsafeItemsLossily() throws {
        let json = #"{"shapes":[],"cards":[{"id":"g1","type":"image","title":"Mixed","items":[
            {"url":"https://example.com/a.jpg"},{"url":"http://example.com/b.jpg"},
            {"url":"https://example.com/c.jpg"}]}]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
        guard case .image(let card) = msg.cards[0] else { return XCTFail("expected an image card") }
        XCTAssertEqual(card.items?.map(\.url), ["https://example.com/a.jpg", "https://example.com/c.jpg"])
    }

    func testGalleryAllBadItemsFallsBackToTopLevelURL() throws {
        let json = #"{"shapes":[],"cards":[{"id":"g2","type":"image","title":"All bad",
            "url":"https://example.com/top.jpg","items":[
            {"url":"http://example.com/b.jpg"},{"url":42}]}]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
        guard case .image(let card) = msg.cards[0] else { return XCTFail("expected an image card") }
        XCTAssertNil(card.items)
        XCTAssertEqual(card.images, [ImageItem(url: "https://example.com/top.jpg")])
    }

    func testVideoURLThatLooksLikeYouTubeBecomesAnID() throws {
        let json = #"{"shapes":[],"cards":[{"id":"v2","type":"video","title":"Link","url":"https://youtu.be/dQw4w9WgXcQ"}]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
        guard case .video(let card) = msg.cards[0] else { return XCTFail("expected a video card") }
        XCTAssertEqual(card.youtubeID, "dQw4w9WgXcQ")
        XCTAssertNil(card.url)
    }

    func testMediaRoundTripKeepsWireKeys() throws {
        for name in ["video.json", "gallery.json"] {
            let data = try Data(contentsOf: Self.fixturesDir.appendingPathComponent(name))
            let msg = try JSONDecoder().decode(HUDMessage.self, from: data)
            let out = try JSONEncoder().encode(msg)
            XCTAssertEqual(try JSONDecoder().decode(HUDMessage.self, from: out), msg, name)
            let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: out) as? [String: Any], name)
            let cardDicts = try XCTUnwrap(raw["cards"] as? [[String: Any]], name)
            let flatKeys = Set(cardDicts.flatMap(\.keys))
            XCTAssertFalse(flatKeys.contains("youtubeID"), name)
            XCTAssertFalse(flatKeys.contains("thumbnailURL"), name)
            XCTAssertFalse(flatKeys.contains("sourcePage"), name)
            if name == "video.json" {
                XCTAssertTrue(flatKeys.contains("youtube_id"), name)
                XCTAssertTrue(flatKeys.contains("thumbnail_url"), name)
                XCTAssertTrue(flatKeys.contains("source_page"), name)
            }
            if name == "gallery.json" {
                let itemKeys = Set(try XCTUnwrap(cardDicts[0]["items"] as? [[String: Any]], name).flatMap(\.keys))
                XCTAssertTrue(itemKeys.contains("source_page"), name)
                XCTAssertFalse(itemKeys.contains("sourcePage"), name)
            }
        }
    }
}
