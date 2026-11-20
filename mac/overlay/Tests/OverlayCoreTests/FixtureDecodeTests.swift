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
        XCTAssertEqual(types, Set(["info", "stat", "bar", "flow", "link", "image", "video", "web", "chart"]))
    }

    /// jfollow10b: the web card (page "map") decodes; an unknown page is skipped like an unknown type.
    func testWebCardDecodesAndUnknownPageIsSkipped() throws {
        let file = try XCTUnwrap(try fixtureFiles().first { $0.lastPathComponent == "web.json" })
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(contentsOf: file))
        guard case .web(let web) = msg.cards.first else { return XCTFail("expected a web card, got \(msg.cards)") }
        XCTAssertEqual(msg.cards.first?.typeTag, "MAP")
        XCTAssertEqual(web.page, "map")
        XCTAssertEqual(web.data.routes.count, 2)
        XCTAssertEqual(web.data.pins.count, 2)
        XCTAssertEqual(web.data.attribution, MapData.osmAttribution)
        let odd = #"{"shapes":[],"cards":[{"id":"w1","type":"web","title":"T","page":"evil","data":{"routes":[],"pins":[{"lat":1,"lon":2,"label":"x"}],"attribution":"x"}},{"id":"i1","type":"info","title":"Yes","body":["kept"]}]}"#
        let out = try JSONDecoder().decode(HUDMessage.self, from: Data(odd.utf8))
        XCTAssertEqual(out.cards.map(\.typeName), ["info"])
    }

    /// jfollow10c criterion 19: the drawing web card and the chart card decode; oversize/unknown are skipped.
    func testDrawingAndChartCards() throws {
        let file = Self.fixturesDir.appendingPathComponent("web_drawing.json")
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(contentsOf: file))
        guard case .web(let web) = msg.cards.first else { return XCTFail("expected a web card, got \(msg.cards)") }
        XCTAssertEqual(web.page, "drawing")
        XCTAssertEqual(msg.cards.first?.typeTag, "DRAWING")
        XCTAssertTrue(try XCTUnwrap(web.drawing).svg.hasPrefix("<svg"))
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(web.drawing).legend.count, 3)
        let back = try JSONDecoder().decode(HUDMessage.self, from: JSONEncoder().encode(msg))
        XCTAssertEqual(back, msg)
        let big = String(repeating: "a", count: 48_001 - 11)
        let oversize = #"{"shapes":[],"cards":[{"id":"d1","type":"web","title":"T","page":"drawing","data":{"svg":"<svg>\#(big)</svg>","legend":[]}},{"id":"i1","type":"info","title":"Yes","body":["kept"]}]}"#
        XCTAssertEqual(try JSONDecoder().decode(HUDMessage.self, from: Data(oversize.utf8)).cards.map(\.typeName), ["info"])
        let charts = try JSONDecoder().decode(HUDMessage.self, from: Data(contentsOf: Self.fixturesDir.appendingPathComponent("chart.json")))
        XCTAssertEqual(charts.cards.count, 8)
        XCTAssertEqual(Set(charts.cards.map(\.typeTag)), ["GRAPH"])
        let radar = #"{"shapes":[],"cards":[{"id":"c1","type":"chart","title":"T","chart":"radar","labels":["A","B"],"series":[{"name":"S","values":[1,2]}]},{"id":"i1","type":"info","title":"Yes","body":["kept"]}]}"#
        XCTAssertEqual(try JSONDecoder().decode(HUDMessage.self, from: Data(radar.utf8)).cards.map(\.typeName), ["info"])
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
        let json = #"{"shapes":[],"cards":[{"id":"g1","type":"image","title":"Mixed","items":[{"url":"https://example.com/a.jpg"},{"url":"http://example.com/b.jpg"},{"url":"https://example.com/c.jpg"}]}]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
        guard case .image(let card) = msg.cards[0] else { return XCTFail("expected an image card") }
        XCTAssertEqual(card.items?.map(\.url), ["https://example.com/a.jpg", "https://example.com/c.jpg"])
    }

    func testGalleryAllBadItemsFallsBackToTopLevelURL() throws {
        let json = #"{"shapes":[],"cards":[{"id":"g2","type":"image","title":"All bad","url":"https://example.com/top.jpg","items":[{"url":"http://example.com/b.jpg"},{"url":42}]}]}"#
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

    func testVideoURLThatIsNotAVideoIsSkipped() throws {
        for url in ["https://example.com/a.html", "https://cdn.example.com/live.m3u8"] {
            let json = #"{"shapes":[],"cards":[{"id":"v3","type":"video","title":"Not a video","url":"URL"}]}"#
                .replacingOccurrences(of: "URL", with: url)
            let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8))
            XCTAssertTrue(msg.cards.isEmpty, url)
        }
    }

    /// jfollow10d criterion 23: the model web card decodes; a bad slug or view is skipped.
    func testModelWebCard() throws {
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(contentsOf: Self.fixturesDir.appendingPathComponent("web-model.json")))
        guard case .web(let web) = msg.cards.first else { return XCTFail("expected a web card, got \(msg.cards)") }
        XCTAssertEqual(web.page, "model")
        XCTAssertEqual(web.model?.slug, "test-mug")
        XCTAssertEqual(web.model?.view, "model")
        XCTAssertEqual(try JSONDecoder().decode(HUDMessage.self, from: JSONEncoder().encode(msg)), msg)
        for data in [#"{"slug":"../x","view":"model"}"#, #"{"slug":"Test","view":"model"}"#, #"{"slug":"a","view":"edit"}"#, #"{"routes":[]}"#] {
            let json = #"{"shapes":[],"cards":[{"id":"m1","type":"web","title":"T","page":"model","data":\#(data)},{"id":"i1","type":"info","title":"Yes","body":["kept"]}]}"#
            XCTAssertEqual(try JSONDecoder().decode(HUDMessage.self, from: Data(json.utf8)).cards.map(\.typeName), ["info"], data)
        }
    }

    /// jfollow10f criterion 24: the places map card decodes with its rows, credits and "as of";
    /// a row with an unknown mode is skipped and the card still decodes; data.json re-encodes the rows.
    func testPlacesMapCardRows() throws {
        let file = Self.fixturesDir.appendingPathComponent("web-places.json")
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(contentsOf: file))
        guard case .web(let web) = msg.cards.first else { return XCTFail("expected a web card, got \(msg.cards)") }
        XCTAssertEqual(web.page, "map")
        XCTAssertEqual(web.data.rows?.count, 5)
        XCTAssertEqual(web.data.credits?.count, 3)
        XCTAssertEqual(web.data.asof, "20:37")
        XCTAssertEqual(web.data.rows?.first?.times?.count, 2)
        let odd = #"{"shapes":[],"cards":[{"id":"w2","type":"web","title":"T","page":"map","data":{"routes":[],"pins":[{"lat":51.5,"lon":-0.12,"label":"Home"}],"attribution":"x","rows":[{"name":"Car Park","km":1,"mode":"car"},{"name":"Kept","km":1.2,"min":7,"mode":"walk","times":[{"at":"25:00","film":"x"},{"at":"18:00","film":"Ebenezer"}]}],"credits":["osm","evil"],"asof":"<b>"}}]}"#
        let oddMsg = try JSONDecoder().decode(HUDMessage.self, from: Data(odd.utf8))
        guard case .web(let w2) = oddMsg.cards.first else { return XCTFail("the card itself must still decode") }
        XCTAssertEqual(w2.data.rows?.map(\.name), ["Kept"])
        XCTAssertEqual(w2.data.rows?.first?.times?.map(\.at), ["18:00"])
        XCTAssertEqual(w2.data.credits, ["osm"])
        XCTAssertNil(w2.data.asof)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(web.data)) as? [String: Any])
        XCTAssertEqual((json["rows"] as? [Any])?.count, 5)
        XCTAssertEqual(json["asof"] as? String, "20:37")
    }

    func testPlacesRowNoteTomorrow() throws {
        let raw = #"{"shapes":[],"cards":[{"id":"w3","type":"web","title":"T","page":"map","data":{"routes":[],"pins":[{"lat":51.5,"lon":-0.12,"label":"Home"}],"attribution":"x","rows":[{"name":"A","km":1,"note":"none_tomorrow"},{"name":"B","km":2,"note":"<b>"}]}}]}"#
        let msg = try JSONDecoder().decode(HUDMessage.self, from: Data(raw.utf8))
        guard case .web(let web) = msg.cards.first else { return XCTFail("expected a web card") }
        XCTAssertEqual(web.data.rows?.map(\.note), ["none_tomorrow", nil])
    }
}
