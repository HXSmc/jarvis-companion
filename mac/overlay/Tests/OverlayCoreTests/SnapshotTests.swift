import XCTest
import SwiftUI
import AppKit
@testable import OverlayCore

/// Renders fixture cards, the fixture shapes and one orb per state to PNG files.
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
                let name = "card-\(card.typeName)-\(card.id ?? "noid").png"
                try renderPNG(
                    CardView(card: card).padding(20).background(Color.black),
                    size: CGSize(width: 420, height: 420),
                    name: name
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
    }

    func testRenderOrbStates() throws {
        for state in OrbState.allCases {
            let view = VStack(spacing: 14) {
                OrbView(state: state, level: 0.7)
                Text(state.rawValue.uppercased())
                    .font(Theme.microLabel)
                    .foregroundColor(.white.opacity(0.6))
            }
            .frame(width: 120, height: 140)
            .background(Color.black)
            try renderPNG(view, size: CGSize(width: 120, height: 140), name: "orb-\(state.rawValue).png")
        }
    }
}
