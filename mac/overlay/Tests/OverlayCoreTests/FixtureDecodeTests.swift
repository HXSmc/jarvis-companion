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
        XCTAssertEqual(types, Set(["info", "stat", "bar", "flow", "link", "image"]))
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
}
