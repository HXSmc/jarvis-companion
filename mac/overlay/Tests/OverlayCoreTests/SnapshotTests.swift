import XCTest
import SwiftUI
import AppKit
@testable import OverlayCore

/// Bright busy desktop stand-in: white with a red block, grey bars and black
/// text lines (shapes only), to check contrast on light backdrops.
private struct BusyLightBackdrop: View {
    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.white
            Rectangle().fill(Theme.accent)
                .frame(width: 150, height: 110)
                .padding(.leading, 24).padding(.top, 20)
            Rectangle().fill(Color(white: 0.87)).frame(height: 12).padding(.top, 148)
            Rectangle().fill(Color(white: 0.87)).frame(height: 12).padding(.top, 168)
            VStack(alignment: .leading, spacing: 9) {
                ForEach(0..<8, id: \.self) { _ in
                    Rectangle().fill(Color.black).frame(height: 2)
                }
            }
            .padding(.leading, 28).padding(.top, 196)
        }
    }
}

/// Renders fixture cards, the fixture shapes and one orb per state to PNG files,
/// each over black and over a bright busy backdrop (`-light`).
@MainActor
final class SnapshotTests: XCTestCase {
    static var outDir: URL {
        let dir = ProcessInfo.processInfo.environment["SNAPSHOT_DIR"].flatMap { $0.isEmpty ? nil : $0 }
            .map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("overlay-snapshots", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func renderPNG<V: View>(_ view: V, size: CGSize, name: String) throws {
        let renderer = ImageRenderer(content: view.frame(width: size.width, height: size.height))
        renderer.scale = 2
        let image = try XCTUnwrap(renderer.nsImage, "no nsImage for \(name)")
        let tiff = try XCTUnwrap(image.tiffRepresentation, "no tiff for \(name)")
        let rep = try XCTUnwrap(NSBitmapImageRep(data: tiff), "no bitmap rep for \(name)")
        let png = try XCTUnwrap(rep.representation(using: .png, properties: [:]), "no png for \(name)")
        let url = Self.outDir.appendingPathComponent(name)
        try png.write(to: url)
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        let byteCount = try XCTUnwrap(attrs[.size] as? NSNumber, "no size for \(name)")
        XCTAssertGreaterThan(byteCount.intValue, 1024, "\(name) is \(byteCount.intValue) bytes")
    }

    func testRenderEveryFixtureCard() throws {
        let files = try FileManager.default.contentsOfDirectory(
            at: FixtureDecodeTests.fixturesDir, includingPropertiesForKeys: nil
        )
        .filter { $0.pathExtension == "json" && $0.lastPathComponent != "schema.json" }
        XCTAssertFalse(files.isEmpty)
        for file in files {
            let msg = try JSONDecoder().decode(HUDMessage.self, from: try Data(contentsOf: file))
            for card in msg.cards {
                let name = "card-\(card.typeName)-\(card.id ?? "noid")"
                try renderPNG(
                    CardView(card: card).padding(20).background(Color.black),
                    size: CGSize(width: 420, height: 420),
                    name: "\(name).png"
                )
                try renderPNG(
                    CardView(card: card).padding(20).background(BusyLightBackdrop()),
                    size: CGSize(width: 420, height: 420),
                    name: "\(name)-light.png"
                )
            }
        }
    }

    func testRenderShapes() throws {
        let msg = try JSONDecoder().decode(
            HUDMessage.self,
            from: try Data(contentsOf: FixtureDecodeTests.fixturesDir.appendingPathComponent("shapes.json"))
        )
        XCTAssertGreaterThanOrEqual(msg.shapes.count, 3)
        let canvas = ZStack(alignment: .topLeading) {
            Color.black
            ForEach(Array(msg.shapes.enumerated()), id: \.offset) { _, shape in
                ShapeView(shape: shape)
            }
        }
        try renderPNG(canvas, size: CGSize(width: 1200, height: 700), name: "shapes.png")
        let lightCanvas = ZStack(alignment: .topLeading) {
            BusyLightBackdrop()
            ForEach(Array(msg.shapes.enumerated()), id: \.offset) { _, shape in
                ShapeView(shape: shape)
            }
        }
        try renderPNG(lightCanvas, size: CGSize(width: 1200, height: 700), name: "shapes-light.png")
    }

    func testRenderOrbStates() throws {
        for state in OrbState.allCases {
            let view = VStack(spacing: 14) {
                OrbView(state: state, level: 0.7)
                Text(state.rawValue.uppercased())
                    .font(Theme.microLabel)
                    .foregroundColor(.white.opacity(0.72))
            }
            .frame(width: 120, height: 140)
            .background(Color.black)
            try renderPNG(view, size: CGSize(width: 120, height: 140), name: "orb-\(state.rawValue).png")
            let light = VStack(spacing: 14) {
                OrbView(state: state, level: 0.7)
                Text(state.rawValue.uppercased())
                    .font(Theme.microLabel)
                    .foregroundColor(.black)
            }
            .frame(width: 120, height: 140)
            .background(BusyLightBackdrop())
            try renderPNG(light, size: CGSize(width: 120, height: 140), name: "orb-\(state.rawValue)-light.png")
        }
    }

    /// Demo cards placed by ScatterLayout on a 1512x982 screen (menu bar 33 pt) with the orb
    /// and, for 6 and 14 cards, one "Sample" highlight; then the 14 parked as chips.
    func testRenderScatter() throws {
        let screen = CGRect(x: 0, y: 0, width: 1512, height: 982)
        let visible = CGRect(x: 0, y: 33, width: 1512, height: 949)
        let sample = HUDShape(kind: .rect, box: [606, 451, 906, 531], label: "Sample")
        var parked: (cards: [HUDCard], frames: [String: CGRect]) = ([], [:])
        for count in [1, 6, 14] {
            let cards = LayoutDebugCommand.demoCards(count: count)
            let shapes = count > 1 ? [sample] : []
            let keepOut = [ScatterLayout.orbKeepOut(screen: screen)]
                + shapes.map { ScatterLayout.shapeKeepOut($0.rect, screenWidth: screen.width) }
            var state = ScatterLayout.LayoutState()
            for card in cards {
                let size = NSHostingView(rootView: CardView(card: card)).fittingSize
                state.cardAdded(card.listKey, size: size, visible: visible, keepOut: keepOut)
            }
            XCTAssertEqual(state.order.count, count)
            try renderScatter(cards: cards, frames: state.frames, shapes: shapes, chips: false, name: "scatter-\(count)")
            parked = (cards, ScatterLayout.chipFrames(keys: state.order, visible: visible, orbOnRight: true))
        }
        try renderScatter(cards: parked.cards, frames: parked.frames, shapes: [], chips: true, name: "scatter-aside-14")
    }

    private func renderScatter(cards: [HUDCard], frames: [String: CGRect], shapes: [HUDShape],
                               chips: Bool, name: String) throws {
        let size = CGSize(width: 1512, height: 982)
        try renderPNG(scatterScene(Color.black, cards: cards, frames: frames, shapes: shapes, chips: chips),
                      size: size, name: "\(name).png")
        try renderPNG(scatterScene(BusyLightBackdrop(), cards: cards, frames: frames, shapes: shapes, chips: chips),
                      size: size, name: "\(name)-light.png")
    }

    private func scatterScene<Backdrop: View>(_ backdrop: Backdrop, cards: [HUDCard], frames: [String: CGRect],
                                              shapes: [HUDShape], chips: Bool) -> some View {
        ZStack(alignment: .topLeading) {
            backdrop
            ForEach(Array(shapes.enumerated()), id: \.offset) { _, shape in
                ShapeView(shape: shape)
            }
            ForEach(cards, id: \.listKey) { card in
                if let frame = frames[card.listKey] {
                    Group {
                        if chips { CardChip(card: card) } else { CardView(card: card) }
                    }
                    .fixedSize()
                    .offset(x: frame.minX, y: frame.minY)
                }
            }
            OrbView(state: .idle, level: 0)
                .offset(x: 1512 - 68, y: 982 - 68)
        }
    }
}
