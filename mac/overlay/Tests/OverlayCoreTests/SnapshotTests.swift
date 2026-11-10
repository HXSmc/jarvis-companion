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
        var overlapLog = ""
        for count in [1, 6, 14, 18] {
            let cards = LayoutDebugCommand.demoCards(count: count)
            let shapes = count > 1 ? [sample] : []
            let keepOut = [ScatterLayout.orbKeepOut(screen: screen)]
                + shapes.map { ScatterLayout.shapeKeepOut($0.rect, screenWidth: screen.width) }
            var state = ScatterLayout.LayoutState()
            for card in cards {
                let size = NSHostingView(rootView: CardView(card: card)).fittingSize
                state.cardAdded(card.listKey, size: size, visible: visible, keepOut: keepOut)
            }
            if count < 18 { XCTAssertEqual(state.order.count, count) }
            let frames = state.order.compactMap { state.frames[$0] }
            var ratios: [String] = []
            for i in frames.indices {
                for j in frames.indices where j > i {
                    let overlap = ScatterLayout.overlapArea(frames[i], frames[j])
                    guard overlap > 0 else { continue }
                    let ratio = overlap / min(ScatterLayout.area(frames[i]), ScatterLayout.area(frames[j]))
                    ratios.append(String(format: "%.3f", Double(ratio)))
                }
            }
            overlapLog += "scatter-\(count): \(ratios.count) overlapping pairs \(ratios)\n"
            guard count < 18 else { continue }
            try renderScatter(cards: cards, frames: state.frames, shapes: shapes, chips: false, name: "scatter-\(count)")
            parked = (cards, ScatterLayout.chipFrames(keys: state.order, visible: visible, orbOnRight: true))
        }
        try renderScatter(cards: parked.cards, frames: parked.frames, shapes: [], chips: true, name: "scatter-aside-14")
        try overlapLog.write(to: Self.outDir.appendingPathComponent("scatter-overlaps.txt"), atomically: true, encoding: .utf8)
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

// MARK: - Media cards (image, gallery, video)

/// Gradient bitmap with seed-varied colours and dark bands; generated in code only.
private func makeMediaImage(width: Int, height: Int, seed: Int) -> CGImage {
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                        bytesPerRow: 0, space: space,
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    let tint = CGFloat(seed % 5) / 4
    let colors = [
        CGColor(red: 0.10 + tint * 0.2, green: 0.12, blue: 0.18, alpha: 1),
        CGColor(red: 0.55 + tint * 0.3, green: 0.20, blue: 0.16, alpha: 1),
    ] as CFArray
    if let gradient = CGGradient(colorsSpace: space, colors: colors, locations: [0, 1]) {
        ctx.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: CGFloat(width), y: CGFloat(height)), options: [])
    }
    ctx.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 0.28))
    for band in 0..<3 {
        ctx.fill(CGRect(x: 0, y: CGFloat(60 + band * 90), width: CGFloat(width), height: 34))
    }
    return ctx.makeImage()!
}

@MainActor
extension SnapshotTests {
    /// Scene size = card size + 40 (the padding) in both axes.
    private func padded(_ size: CGSize) -> CGSize {
        CGSize(width: size.width + 40, height: size.height + 40)
    }

    /// One media card view over black and over the busy light backdrop.
    private func renderMediaCard<V: View>(_ view: V, size: CGSize, name: String) throws {
        try renderPNG(view.padding(20).background(Color.black), size: size, name: "\(name).png")
        try renderPNG(view.padding(20).background(BusyLightBackdrop()), size: size, name: "\(name)-light.png")
    }

    func testRenderMediaImageCardStates() throws {
        let cards = LayoutDebugCommand.demoMediaCards()
        guard let photo = cards.first(where: { $0.id == "media-1" }), case .image(let card) = photo else {
            XCTFail("demoMediaCards has no media-1 image card")
            return
        }
        guard let item = card.images.first else {
            XCTFail("media-1 has no image item")
            return
        }
        let placeholder = padded(MediaPolicy.cardSize(for: photo)!)
        try renderMediaCard(ImageCardView(card: card), size: placeholder, name: "media-image-loading")

        let states = [item.url: MediaImageState.loaded(makeMediaImage(width: 1200, height: 800, seed: 1))]
        let loaded = padded(MediaPolicy.cardSize(for: photo, imagePixels: CGSize(width: 1200, height: 800))!)
        try renderMediaCard(ImageCardView(card: card, states: states), size: loaded, name: "media-image-loaded")

        let fox = ImageCard(id: "fox", title: "Fox", url: "https://example.com/fox.jpg",
                            alt: "Red fox in snow",
                            sourcePage: "https://commons.wikimedia.org/wiki/File:Example.jpg")
        try renderMediaCard(
            ImageCardView(card: fox, states: [fox.url ?? "": .failed]),
            size: padded(MediaPolicy.cardSize(for: .image(fox))!),
            name: "media-image-failed"
        )
    }

    func testRenderMediaGalleries() throws {
        let cards = LayoutDebugCommand.demoMediaCards()
        guard let gallery = cards.first(where: { $0.id == "media-2" }), case .image(let card) = gallery else {
            XCTFail("demoMediaCards has no media-2 image card")
            return
        }
        var states: [String: MediaImageState] = [:]
        for (index, item) in card.images.enumerated() {
            states[item.url] = index == 3
                ? .failed
                : .loaded(makeMediaImage(width: 900, height: 900, seed: index + 2))
        }
        try renderMediaCard(ImageCardView(card: card, states: states),
                            size: padded(MediaPolicy.cardSize(for: gallery)!),
                            name: "media-gallery-4")

        let items = (1...6).map { ImageItem(url: "https://example.com/frame-\($0).jpg", alt: "Frame \($0)") }
        var sixStates: [String: MediaImageState] = [:]
        for (index, item) in items.enumerated() {
            switch index {
            case 4: break // left as .loading (missing key)
            case 5: sixStates[item.url] = .failed
            default: sixStates[item.url] = .loaded(makeMediaImage(width: 700, height: 500, seed: index + 10))
            }
        }
        let six = ImageCard(id: "six", title: "Six frames", items: items)
        try renderMediaCard(ImageCardView(card: six, states: sixStates),
                            size: padded(MediaPolicy.cardSize(for: .image(six))!),
                            name: "media-gallery-6")
    }

    func testRenderMediaVideoCards() throws {
        let cards = LayoutDebugCommand.demoMediaCards()
        guard let youTube = cards.first(where: { $0.id == "media-3" }),
              let file = cards.first(where: { $0.id == "media-4" }),
              case .video(let youTubeCard) = youTube,
              case .video(let fileCard) = file else {
            XCTFail("demoMediaCards has no media-3/media-4 video cards")
            return
        }
        let thumb = MediaImageState.loaded(makeMediaImage(width: 1600, height: 900, seed: 7))
        let size = padded(MediaPolicy.cardSize(for: youTube)!)
        try renderMediaCard(VideoCardView(card: youTubeCard, thumbnail: thumb, phase: .idle, onPlay: {}),
                            size: size, name: "media-video-thumb")
        try renderMediaCard(VideoCardView(card: youTubeCard, thumbnail: thumb, phase: .failed, onPlay: {}),
                            size: size, name: "media-video-fallback")
        try renderMediaCard(VideoCardView(card: fileCard, thumbnail: nil, phase: .idle, onPlay: {}),
                            size: padded(MediaPolicy.cardSize(for: file)!),
                            name: "media-video-file-thumb")
    }

    func testRenderMediaScatter() throws {
        let screen = CGRect(x: 0, y: 0, width: 1512, height: 982)
        let visible = CGRect(x: 0, y: 33, width: 1512, height: 949)
        let cards = LayoutDebugCommand.demoMediaCards() + LayoutDebugCommand.demoCards(count: 4)
        var states: [String: MediaImageState] = [:]
        for card in cards {
            guard case .image(let p) = card else { continue }
            for item in p.images {
                states[item.url] = .loaded(makeMediaImage(width: 1600, height: 900, seed: 9))
            }
        }
        let keepOut = [ScatterLayout.orbKeepOut(screen: screen)]
        var layout = ScatterLayout.LayoutState()
        for card in cards {
            let size = NSHostingView(rootView: CardView(card: card)).fittingSize
            layout.cardAdded(card.listKey, size: size, visible: visible, keepOut: keepOut)
        }
        XCTAssertEqual(layout.order.count, cards.count)
        let size = CGSize(width: 1512, height: 982)
        try renderPNG(mediaScene(Color.black, cards: cards, frames: layout.frames, states: states),
                      size: size, name: "media-scatter.png")
        try renderPNG(mediaScene(BusyLightBackdrop(), cards: cards, frames: layout.frames, states: states),
                      size: size, name: "media-scatter-light.png")
    }

    private func mediaScene<Backdrop: View>(_ backdrop: Backdrop, cards: [HUDCard],
                                            frames: [String: CGRect], states: [String: MediaImageState]) -> some View {
        ZStack(alignment: .topLeading) {
            backdrop
            ForEach(cards, id: \.listKey) { card in
                if let frame = frames[card.listKey] {
                    Group {
                        switch card {
                        case .image(let p):
                            ImageCardView(card: p, states: states)
                        case .video(let p):
                            VideoCardView(card: p,
                                          thumbnail: .loaded(makeMediaImage(width: 1600, height: 900, seed: 9)),
                                          phase: .idle, onPlay: {})
                        default:
                            CardView(card: card)
                        }
                    }
                    .fixedSize()
                    .offset(x: frame.minX, y: frame.minY)
                }
            }
            OrbView(state: .idle, level: 0)
                .offset(x: 1512 - 68, y: 982 - 68)
        }
    }

    func testMediaCardFittingSizesMatchPolicy() throws {
        for card in LayoutDebugCommand.demoMediaCards() {
            let expected = try XCTUnwrap(MediaPolicy.cardSize(for: card), "no cardSize for \(card.listKey)")
            let size = NSHostingView(rootView: CardView(card: card)).fittingSize
            XCTAssertEqual(size.width, MediaPolicy.cardWidth, accuracy: 1, card.listKey)
            XCTAssertEqual(size.height, expected.height, accuracy: 1, card.listKey)
        }
    }

    /// Round 1: the play button never covers the preview's own text; galleries of 3 and 5 have
    /// no empty cell; the "Starting video" cover after the click.
    func testRenderMediaRoundOneStates() throws {
        guard case .video(let youTube)? = LayoutDebugCommand.demoMediaCards().first(where: { $0.id == "media-3" }) else {
            return XCTFail("demoMediaCards has no media-3 video card")
        }
        let size = padded(MediaPolicy.cardSize(for: .video(youTube))!)
        try renderMediaCard(VideoCardView(card: youTube, thumbnail: .loading, phase: .idle, onPlay: {}),
                            size: size, name: "media-video-thumb-loading")
        try renderMediaCard(VideoCardView(card: youTube, thumbnail: .failed, phase: .idle, onPlay: {}),
                            size: size, name: "media-video-thumb-failed")
        let thumb = MediaImageState.loaded(makeMediaImage(width: 1600, height: 900, seed: 3))
        try renderMediaCard(VideoCardView(card: youTube, thumbnail: thumb, phase: .starting, onPlay: {}) { Color.black },
                            size: size, name: "media-video-starting")
        for count in [3, 5] {
            let items = (1...count).map { ImageItem(url: "https://example.com/g\(count)-\($0).jpg") }
            var states: [String: MediaImageState] = [:]
            for (index, item) in items.enumerated() {
                states[item.url] = .loaded(makeMediaImage(width: 900, height: 700, seed: index + count))
            }
            let card = ImageCard(id: "g\(count)", title: "\(count) frames", items: items)
            try renderMediaCard(ImageCardView(card: card, states: states),
                                size: padded(MediaPolicy.cardSize(for: .image(card))!),
                                name: "media-gallery-\(count)")
        }
    }

    /// Round 2: while .starting the cover is opaque, with or without a thumbnail. The player is a
    /// white stand-in for a loading web page; a translucent cover would let it show through.
    func testStartingCoverHidesThePlayer() throws {
        guard case .video(let youTube)? = LayoutDebugCommand.demoMediaCards().first(where: { $0.id == "media-3" }) else {
            return XCTFail("demoMediaCards has no media-3 video card")
        }
        let thumb = MediaImageState.loaded(makeMediaImage(width: 1600, height: 900, seed: 5))
        for (name, thumbnail) in [("thumb", Optional(thumb)), ("none", nil)] {
            let view = VideoCardView(card: youTube, thumbnail: thumbnail, phase: .starting, onPlay: {}) { Color.white }
            let size = try XCTUnwrap(MediaPolicy.cardSize(for: .video(youTube)))
            let renderer = ImageRenderer(content: view.frame(width: size.width, height: size.height))
            renderer.scale = 1
            let image = try XCTUnwrap(renderer.cgImage, "no image for \(name)")
            // Sample the area's lower-left quarter, away from the centred "Starting video" text.
            let x = Int(MediaPolicy.cardWidth * 0.15)
            let y = Int(MediaPolicy.titleBarHeight + MediaPolicy.videoAreaSize.height * 0.85)
            var pixel = [UInt8](repeating: 0, count: 4)
            let ctx = try XCTUnwrap(CGContext(data: &pixel, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                              space: CGColorSpaceCreateDeviceRGB(),
                                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            ctx.draw(image, in: CGRect(x: -x, y: -(image.height - 1 - y), width: image.width, height: image.height))
            XCTAssertLessThan(Int(pixel[0]) + Int(pixel[1]) + Int(pixel[2]), 3 * 120,
                              "starting cover lets the player show through (\(name)): \(pixel)")
            try renderMediaCard(view, size: padded(size), name: "media-video-starting-\(name)")
        }
    }
}
