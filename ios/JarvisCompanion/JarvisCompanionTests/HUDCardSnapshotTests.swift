import SwiftUI
import UIKit
import XCTest
@testable import JarvisCompanion

/// HUD cards (common/hud.py + tests/fixtures/hud/*.json, copied below): the gateway JSON
/// decodes with the same clamps, and every card type is drawn in a real simulator window to
/// PNG ($HUD_SHOTS_DIR, set by CI through TEST_RUNNER_HUD_SHOTS_DIR; without it the shot
/// still renders, nothing is written). No network: image placeholders are fine.
@MainActor
final class HUDCardSnapshotTests: XCTestCase {

    static let info = """
    {"shapes": [], "cards": [
      {"id": "c-info", "type": "info", "title": "Why the retry loop changed", "body":
        ["The old loop retried forever on a 401.",
         "Now it stops after 3 tries.",
         "Tokens refresh before the retry."]}]}
    """

    static let stat = """
    {"shapes": [], "cards": [
      {"id": "c-stat", "type": "stat", "title": "Files changed", "value": "14", "label": "since last merge"}]}
    """

    static let bar = """
    {"shapes": [], "cards": [
      {"id": "c-bar", "type": "bar", "title": "Lines changed by folder",
       "items": [{"label": "gateway", "value": 120}, {"label": "mac", "value": 64},
                  {"label": "tests", "value": 210.5}],
       "caption": "git diff --stat since the last merge"}]}
    """

    static let flow = """
    {"shapes": [], "cards": [
      {"id": "c-flow", "type": "flow", "title": "Request path",
       "nodes": [{"id": "a", "text": "Wake word"}, {"id": "b", "text": "Speech to text"},
                  {"id": "c", "text": "Brain"}, {"id": "d", "text": "Overlay"}],
       "edges": [["a", "b"], ["b", "c", "final text"], ["c", "d", "shapes + cards"]]}]}
    """

    static let link = """
    {"shapes": [], "cards": [
      {"id": "c-link", "type": "link", "title": "VS Code command line",
       "url": "https://code.visualstudio.com/docs/configure/command-line",
       "source": "code.visualstudio.com"}]}
    """

    static let image = """
    {"shapes": [], "cards": [
      {"id": "c-img", "type": "image", "title": "Board photo",
       "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Example.jpg"},
      {"id": "c-img2", "type": "image", "title": "Saved frame", "file_id": "frame_0042.jpg"}]}
    """

    static let layer = """
    {"shapes": [], "cards": [
      {"id": "c-info", "type": "info", "title": "Why the retry loop changed", "body":
        ["The old loop retried forever on a 401.", "Now it stops after 3 tries."]},
      {"id": "c-stat", "type": "stat", "title": "Files changed", "value": "14", "label": "since last merge"},
      {"id": "c-bar", "type": "bar", "title": "Lines changed by folder",
       "items": [{"label": "gateway", "value": 120}, {"label": "tests", "value": 210.5}],
       "caption": "git diff --stat since the last merge"},
      {"id": "c-flow", "type": "flow", "title": "Request path",
       "nodes": [{"id": "a", "text": "Wake word"}, {"id": "b", "text": "Speech to text"},
                  {"id": "c", "text": "Brain"}, {"id": "d", "text": "Overlay"}],
       "edges": [["a", "b"], ["b", "c", "final text"], ["c", "d"]]},
      {"id": "c-link", "type": "link", "title": "VS Code command line",
       "url": "https://code.visualstudio.com/docs/configure/command-line", "source": "code.visualstudio.com"},
      {"id": "c-img", "type": "image", "title": "Board photo",
       "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Example.jpg"}]}
    """

    // MARK: decoding (same rules as common/hud.py)

    private func cards(_ json: String) throws -> [HUDCard] {
        let msg = try JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]
        return HUDCard.list(from: msg?["cards"])
    }

    func testFixturesDecode() throws {
        let info = try cards(Self.info)
        XCTAssertEqual(info.count, 1)
        XCTAssertEqual(info[0].title, "Why the retry loop changed")
        XCTAssertEqual(info[0].body, ["The old loop retried forever on a 401.",
                                      "Now it stops after 3 tries.",
                                      "Tokens refresh before the retry."])

        let stat = try cards(Self.stat)
        XCTAssertEqual(stat[0].value, "14")
        XCTAssertEqual(stat[0].statLabel, "since last merge")

        let bar = try cards(Self.bar)
        XCTAssertEqual(bar[0].items?.map(\.label), ["gateway", "mac", "tests"])
        XCTAssertEqual(bar[0].items?.map(\.value), [120, 64, 210.5])
        XCTAssertEqual(bar[0].caption, "git diff --stat since the last merge")

        let flow = try cards(Self.flow)
        XCTAssertEqual(flow[0].nodes?.map(\.text), ["Wake word", "Speech to text", "Brain", "Overlay"])
        XCTAssertEqual(flow[0].edges?.count, 3)
        XCTAssertEqual(flow[0].edges?[1].label, "final text")

        let link = try cards(Self.link)
        XCTAssertEqual(link[0].url?.host, "code.visualstudio.com")
        XCTAssertEqual(link[0].source, "code.visualstudio.com")

        let image = try cards(Self.image)
        XCTAssertEqual(image.count, 2)  // url card + file_id card, both kept
        XCTAssertEqual(image[0].url?.host, "upload.wikimedia.org")
        XCTAssertNil(image[1].url)
        XCTAssertEqual(image[1].fileId, "frame_0042.jpg")
    }

    func testInvalidCardsAreDropped() {
        XCTAssertNil(HUDCard.parse(["id": "x", "type": "mystery", "title": "No such type"]))
        XCTAssertNil(HUDCard.parse(["id": "x", "type": "link", "title": "Not https",
                                    "url": "http://example.com/a"]))  // http is dropped
        XCTAssertNil(HUDCard.parse(["id": "x", "type": "link", "title": "No host", "url": "https:///path"]))
        XCTAssertNil(HUDCard.parse(["id": "x", "type": "info", "title": "", "body": ["a"]]))  // empty title
        XCTAssertNil(HUDCard.parse(["id": "x", "type": "stat", "title": "No value", "value": ""]))
        XCTAssertNil(HUDCard.parse(["id": "x", "type": "image", "title": "Neither url nor file", "url": "ftp://x/y"]))
        // text clamps like common/hud.py
        let long = String(repeating: "a", count: 100)
        XCTAssertEqual(HUDCard.parse(["id": "x", "type": "stat", "title": long, "value": "1"])?.title.count, 60)
        // list keeps valid cards only and caps at 6
        let one: [String: Any] = ["id": "n", "type": "info", "title": "t", "body": ["b"]]
        XCTAssertEqual(HUDCard.list(from: Array(repeating: one, count: 9)).count, 6)
        XCTAssertEqual(HUDCard.list(from: [one, ["id": "y", "type": "bad", "title": "t"]]).count, 1)
    }

    // MARK: layer layout

    func testSlotsAvoidTheSubjectAndControls() {
        let size = CGSize(width: 390, height: 844)
        let avoid = CGRect(x: 140, y: 300, width: 120, height: 160)  // subject, mid-screen
        let slots = HUDLayer.slots(in: size, avoid: avoid)
        XCTAssertFalse(slots.isEmpty)
        for s in slots {
            XCTAssertFalse(s.insetBy(dx: -8, dy: -8).intersects(avoid), "slot \(s) covers the subject")
            XCTAssertGreaterThanOrEqual(s.minY, 70)              // status bar area stays free
            XCTAssertLessThanOrEqual(s.maxY, size.height - 150)  // bottom controls stay free
            XCTAssertLessThanOrEqual(s.width, size.width * 0.46 + 0.5)  // <= ~46% width
        }
        XCTAssertEqual(slots.count, 6)  // slots over the subject are trimmed beside / above / below it
        // a subject filling the middle band: side slots there are too narrow, so they are dropped
        let wide = CGRect(x: 60, y: 250, width: 270, height: 300)
        for s in HUDLayer.slots(in: size, avoid: wide) {
            XCTAssertFalse(s.insetBy(dx: -8, dy: -8).intersects(wide), "slot \(s) covers the subject")
        }
        XCTAssertGreaterThanOrEqual(HUDLayer.slots(in: size, avoid: nil).count, 6)
    }

    // MARK: screenshots (one per type + the composed layer)

    func testInfoCardShot() throws {
        let card = try XCTUnwrap(cards(Self.info).first)
        XCTAssertEqual(card.body?.count, 3)
        try shot("hud-01-info", card: card)
    }

    func testStatCardShot() throws {
        try shot("hud-02-stat", card: try XCTUnwrap(cards(Self.stat).first))
    }

    func testBarCardShot() throws {
        try shot("hud-03-bar", card: try XCTUnwrap(cards(Self.bar).first))
    }

    func testFlowCardShot() throws {
        try shot("hud-04-flow", card: try XCTUnwrap(cards(Self.flow).first))
    }

    func testLinkCardShot() throws {
        try shot("hud-05-link", card: try XCTUnwrap(cards(Self.link).first))
    }

    func testImageCardShot() throws {
        let image = try cards(Self.image)
        try shot("hud-06-image", card: try XCTUnwrap(image.first))
        try shot("hud-06b-image-file", card: try XCTUnwrap(image.last))  // file_id: quiet fallback
    }

    func testLayerShot() throws {
        let all = try cards(Self.layer)
        XCTAssertEqual(all.count, 6)
        let avoid = CGRect(x: 140, y: 300, width: 120, height: 160)
        // The yellow box marks where the subject is; no card may cover it.
        let view = ZStack {
            Color(white: 0.08)
            Rectangle()
                .stroke(Color.yellow, lineWidth: 3)
                .frame(width: avoid.width, height: avoid.height)
                .position(x: avoid.midX, y: avoid.midY)
            HUDLayer(cards: all, avoid: avoid)
        }
        try draw("hud-07-layer", AnyView(view), size: CGSize(width: 390, height: 844))
    }

    // MARK: row budget (whole rows only, never a mid-row cut)

    func testFittingRowsKeepsWholeLeadingRows() {
        XCTAssertEqual(HUDCardView.fittingRows(heights: [50, 50, 50], budget: 110, moreHeight: 17), 1)
        XCTAssertEqual(HUDCardView.fittingRows(heights: [50, 50], budget: 110, moreHeight: 17), 2)  // all fit, no reserve
        XCTAssertEqual(HUDCardView.fittingRows(heights: [50, 50, 50], budget: nil, moreHeight: 17), 3)  // no slot: all
        XCTAssertEqual(HUDCardView.fittingRows(heights: [50], budget: 10, moreHeight: 17), 1)  // one row even in a tiny slot
    }

    func testFlowCardTwelveNodesInA200PointSlotShowsMoreLine() {
        var card = HUDCard(id: "f", type: "flow", title: "Request path")
        card.nodes = (0..<12).map { HUDCard.FlowNode(id: "n\($0)", text: "Node \($0)") }
        let view = HUDCardView(card: card, maxHeight: 200)
        let fit = view.flowFit
        XCTAssertGreaterThanOrEqual(fit.nodes, 1)   // at least one whole node
        XCTAssertLessThan(fit.nodes, 12)            // a 200 pt slot cannot hold 12
        XCTAssertEqual(fit.dropped, 12 - fit.nodes) // no extra-edge lines here
        XCTAssertEqual(view.moreLineText, "+\(fit.dropped) more")
        XCTAssertNil(HUDCardView(card: card, maxHeight: 900).moreLineText)  // a tall slot shows all
        XCTAssertNil(HUDCardView(card: card).moreLineText)                  // no slot: never drops
    }

    // MARK: bright / busy and Dynamic Type variants (round-1 review)

    /// Every card type over a bright/busy camera frame stand-in: black 0.8 fill, white
    /// 0.85 text must still read (contrast check, not pixel assert).
    func testBrightBackdropCardShots() throws {
        try shotVariant("hud-01b-info-bright", card: try XCTUnwrap(cards(Self.info).first), busy: true)
        try shotVariant("hud-02b-stat-bright", card: try XCTUnwrap(cards(Self.stat).first), busy: true)
        try shotVariant("hud-03b-bar-bright", card: try XCTUnwrap(cards(Self.bar).first), busy: true)
        try shotVariant("hud-04b-flow-bright", card: try XCTUnwrap(cards(Self.flow).first), busy: true)
        try shotVariant("hud-05b-link-bright", card: try XCTUnwrap(cards(Self.link).first), busy: true)
        try shotVariant("hud-06c-image-bright", card: try XCTUnwrap(cards(Self.image).first), busy: true)
    }

    /// Every card type at accessibility3 (uncapped; the layer itself caps at
    /// accessibility2): sizes scale, nothing falls below the 11 pt floor.
    func testAccessibility3CardShots() throws {
        try shotVariant("hud-01c-info-ax3", card: try XCTUnwrap(cards(Self.info).first), ax3: true)
        try shotVariant("hud-02c-stat-ax3", card: try XCTUnwrap(cards(Self.stat).first), ax3: true)
        try shotVariant("hud-03c-bar-ax3", card: try XCTUnwrap(cards(Self.bar).first), ax3: true)
        try shotVariant("hud-04c-flow-ax3", card: try XCTUnwrap(cards(Self.flow).first), ax3: true)
        try shotVariant("hud-05c-link-ax3", card: try XCTUnwrap(cards(Self.link).first), ax3: true)
        try shotVariant("hud-06d-image-ax3", card: try XCTUnwrap(cards(Self.image).first), ax3: true)
    }

    // MARK: drawing (same real-window approach as CardSnapshotTests)

    private func shot(_ name: String, card: HUDCard) throws {
        // Card at a real slot size: ~46% of a 390 pt phone width.
        let view = HUDCardView(card: card)
            .frame(width: 179, height: 200)
            .background(Color(white: 0.08))
        try draw(name, AnyView(view), size: CGSize(width: 390, height: 260))
    }

    /// Variant shot: over a bright/busy backdrop, or at accessibility3 type size with a
    /// tall uncapped canvas (the layer caps at accessibility2, the card alone must not).
    private func shotVariant(_ name: String, card: HUDCard, busy: Bool = false, ax3: Bool = false) throws {
        let view = ZStack(alignment: .top) {
            if busy { Self.busyBackdrop } else { Color(white: 0.08) }
            Group {
                if ax3 {
                    HUDCardView(card: card).dynamicTypeSize(.accessibility3)
                } else {
                    HUDCardView(card: card)
                }
            }
            .frame(width: 179, alignment: .top)
            .padding(.top, 16)
        }
        try draw(name, AnyView(view), size: CGSize(width: 390, height: ax3 ? 820 : 320))
    }

    /// White with grey stripes: the bright/busy camera frame a card must stay legible over.
    private static var busyBackdrop: some View {
        GeometryReader { geo in
            Path { p in
                var y: CGFloat = 0
                while y < geo.size.height {
                    p.addRect(CGRect(x: 0, y: y, width: geo.size.width, height: 12))
                    y += 24
                }
            }
            .fill(Color(white: 0.72))
        }
        .background(Color.white)
    }

    /// $HUD_SHOTS_DIR when set; otherwise the shot still renders but nothing is written.
    private func outputDir() throws -> URL? {
        guard let env = ProcessInfo.processInfo.environment["HUD_SHOTS_DIR"] else { return nil }
        let dir = URL(fileURLWithPath: env)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func draw(_ name: String, _ view: AnyView, size: CGSize) throws {
        let host = UIHostingController(rootView: view.preferredColorScheme(.dark))
        host.overrideUserInterfaceStyle = .dark
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        let window = scene.map { UIWindow(windowScene: $0) } ?? UIWindow()
        window.frame = CGRect(origin: .zero, size: size)
        window.rootViewController = host
        window.isHidden = false
        host.view.frame = window.bounds
        host.view.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.8))  // let SwiftUI + the layer fade-in settle
        let format = UIGraphicsImageRendererFormat()
        format.scale = 3
        let image = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        window.isHidden = true
        let png = try XCTUnwrap(image.pngData())
        if let dir = try outputDir() {
            try png.write(to: dir.appendingPathComponent("\(name).png"))
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
