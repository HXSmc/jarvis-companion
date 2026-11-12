import CoreGraphics
import XCTest
@testable import OverlayCore

final class CardSizeTests: XCTestCase {
    /// 360x240 into 1440x900: capped by height (720), keeps its 1.5 aspect, centred.
    func testEnlargedFrameCentredAndCapped() {
        let frame = CardSize.enlargedFrame(natural: CGSize(width: 360, height: 240),
                                           visible: CGRect(x: 0, y: 0, width: 1440, height: 900))
        XCTAssertEqual(frame.midX, 720, accuracy: 0.5)
        XCTAssertEqual(frame.midY, 450, accuracy: 0.5)
        XCTAssertEqual(frame.width / frame.height, 1.5, accuracy: 1.5 * 0.005)
        XCTAssertLessThanOrEqual(frame.width, 1152.001)
        XCTAssertLessThanOrEqual(frame.height, 720.001)
        XCTAssertTrue(min(abs(frame.width - 1152), abs(frame.height - 720)) <= 0.5,
                      "one dimension must sit at its cap")
        let offset = CardSize.enlargedFrame(natural: CGSize(width: 360, height: 240),
                                            visible: CGRect(x: 100, y: 50, width: 1440, height: 900))
        XCTAssertEqual(offset.midX, 100 + 720, accuracy: 0.5)
        XCTAssertEqual(offset.midY, 50 + 450, accuracy: 0.5)
    }

    func testEnlargeState() {
        var state = EnlargeState()
        XCTAssertNil(state.max("a"))
        XCTAssertEqual(state.max("b"), "a")
        XCTAssertEqual(state.id, "b")
        state.restore()
        XCTAssertNil(state.id)
        XCTAssertNil(state.max("a"))
        state.dropped(ids: ["a"])
        XCTAssertNil(state.id)
        XCTAssertNil(state.max("q"))
        state.dropped(ids: ["z"])
        XCTAssertEqual(state.id, "q")
    }

    /// Mac live run 1: the enlarged phone diagram cut every label ("Batte...", "Memory and stora...").
    /// Nodes grow with the font, and an enlarged node has more room per word than the tile's.
    @MainActor func testEnlargedFlowNodesGrowWithTheirText() {
        let names = ["Battery", "Camera and sensors", "Processor (SoC)", "Memory and storage",
                     "Touchscreen", "Modem, Wi-Fi, Bluetooth", "Speaker and microphone"]
        let nodes = names.enumerated().map { FlowNode(id: "n\($0.offset)", text: $0.element) }
        let edges = [FlowEdge(from: "n0", to: "n2", label: "power"), FlowEdge(from: "n1", to: "n2", label: "input"),
                     FlowEdge(from: "n2", to: "n3", label: "data"), FlowEdge(from: "n2", to: "n4", label: "display"),
                     FlowEdge(from: "n2", to: "n5", label: "network"), FlowEdge(from: "n2", to: "n6", label: "sound")]
        let card = FlowCard(title: "How a phone works", nodes: nodes, edges: edges)
        let scale = CardView.fontScale(card: .flow(card), width: 1152)
        XCTAssertLessThanOrEqual(scale, CardSize.flowFontScaleCap)
        let tile = FlowBody.rects(card: card, size: CGSize(width: 330, height: 146), scale: 1)
        let big = FlowBody.rects(card: card, size: CGSize(width: 1120, height: 146 * scale), scale: scale)
        XCTAssertEqual(big.count, nodes.count)
        for node in nodes {
            guard let t = tile[node.id], let b = big[node.id] else { return XCTFail(node.id) }
            XCTAssertEqual(b.height, t.height * scale, accuracy: 0.5, "\(node.text) height grows with its font")
            XCTAssertGreaterThan(b.width / scale, t.width * 1.5, "\(node.text) gets more room per word")
        }
    }

    /// The idle clear waits while a card is enlarged (live: it took the enlarged diagram).
    func testEnlargedCardHoldsTheIdleClear() {
        var state = EnlargeState()
        XCTAssertFalse(state.holdsIdle)
        _ = state.max("a")
        XCTAssertTrue(state.holdsIdle)
        state.restore()
        XCTAssertFalse(state.holdsIdle)
    }
    // MARK: Round 5 (Mac live run 2)

    /// The enlarged window is the content's own size, centred: scaled up it was wider than the card
    /// and the card sat left of centre (live: centre x 603 of 1440).
    func testWindowFrameIsTheContentCentred() {
        let visible = CGRect(x: 0, y: 25, width: 1440, height: 875)
        let frame = CardSize.windowFrame(content: CGSize(width: 724, height: 482), visible: visible)
        XCTAssertEqual(frame.width, 724, accuracy: 0.5)
        XCTAssertEqual(frame.height, 482, accuracy: 0.5)
        XCTAssertEqual(frame.midX, 720, accuracy: 0.5)
        XCTAssertEqual(frame.midY, visible.midY, accuracy: 0.5)
        let tall = CardSize.windowFrame(content: CGSize(width: 800, height: 1400), visible: visible)
        XCTAssertLessThanOrEqual(tall.height, 700.001)
        XCTAssertEqual(tall.width / tall.height, 800.0 / 1400.0, accuracy: 0.005)
        XCTAssertEqual(tall.midX, 720, accuracy: 0.5)
    }

    /// The live phone diagram: six labels fan out of "Processor (SoC)". Placed labels never overlap
    /// each other or a node, at the tile and at the enlarged scale; most of them stay.
    func testFlowEdgeLabelsNeverOverlap() {
        let names = ["Battery", "Power management", "Processor (SoC)", "RAM and storage", "Display and touch",
                     "Cameras", "Sensors", "Modem and radios", "Mic and speaker", "Antennas"]
        let nodes = names.enumerated().map { FlowNode(id: "n\($0.offset)", text: $0.element) }
        let edges = [FlowEdge(from: "n0", to: "n1", label: "power"), FlowEdge(from: "n1", to: "n2", label: "power"),
                     FlowEdge(from: "n2", to: "n3", label: "data"), FlowEdge(from: "n2", to: "n4", label: "image"),
                     FlowEdge(from: "n2", to: "n5", label: "image"), FlowEdge(from: "n2", to: "n6", label: "readings"),
                     FlowEdge(from: "n2", to: "n7", label: "data"), FlowEdge(from: "n2", to: "n8", label: "sound"),
                     FlowEdge(from: "n7", to: "n9", label: "signal")]
        // The tile, the live enlarged card (724 pt wide: about 700 for the flow) and a wide one.
        for (width, scale) in [(CGFloat(330), CGFloat(1)), (CGFloat(700), CardSize.flowFontScaleCap),
                               (CGFloat(1120), CardSize.flowFontScaleCap)] {
            let unscaled = FlowLayout.layout(nodes: nodes, edges: edges,
                                             size: CGSize(width: width / scale, height: 300))
            let rects = unscaled.mapValues { CGRect(x: $0.minX * scale, y: $0.minY * scale,
                                                    width: $0.width * scale, height: $0.height * scale) }
            let input = edges.map { e in
                (from: rects[e.from]!, to: rects[e.to]!, size: FlowEdgeLabels.size(e.label!, scale: scale))
            }
            let spots = FlowEdgeLabels.place(input, nodes: Array(rects.values), gap: 6 * scale)
            var boxes: [CGRect] = []
            for (e, spot) in zip(input, spots) {
                guard let spot else { continue }
                let box = CGRect(x: spot.x, y: spot.y - e.size.height / 2, width: e.size.width, height: e.size.height)
                XCTAssertFalse(boxes.contains { $0.intersects(box) }, "labels overlap at scale \(scale)")
                XCTAssertFalse(rects.values.contains { $0.intersects(box) }, "label on a node at scale \(scale)")
                boxes.append(box)
            }
            XCTAssertGreaterThanOrEqual(boxes.count, 5, "scale \(scale): \(boxes.count) of 9 labels kept")
            // Before round 5 every label sat at its edge's middle: the fan-out labels overlapped.
            guard width < 1000 else { continue }
            let middles = input.map { e in
                CGRect(x: (e.from.midX + e.to.midX) / 2 + 6 * scale, y: (e.from.midY + e.to.midY) / 2 - e.size.height / 2,
                       width: e.size.width, height: e.size.height)
            }
            XCTAssertTrue(middles.indices.contains { i in middles.indices.contains { j in i < j && middles[i].intersects(middles[j]) } })
        }
    }

    // MARK: Round 8 (Mac live run 4)

    /// The live run 4 phone diagram: six nodes in one row. Enlarged at the tile's shape (725 pt) the
    /// nodes had 54 pt of text and cut "Power management", "Memory and storage", "Touchscreen". The
    /// enlarged flow is now as wide as its labels need (inside 80 % of the screen), so every label
    /// fits in whole words on two lines.
    func testEnlargedFlowGivesEveryLabelItsWords() {
        let names = ["Battery", "Power management", "Processor (SoC)", "Memory and storage", "Touchscreen display",
                     "Cameras", "Sensors", "Modem and radios", "Mic and speaker", "Antennas"]
        let nodes = names.enumerated().map { FlowNode(id: "n\($0.offset)", text: $0.element) }
        let edges = [FlowEdge(from: "n0", to: "n1", label: "power"), FlowEdge(from: "n1", to: "n2", label: "regulated power"),
                     FlowEdge(from: "n2", to: "n3", label: "data"), FlowEdge(from: "n2", to: "n4", label: "image and touch"),
                     FlowEdge(from: "n2", to: "n5", label: "image"), FlowEdge(from: "n2", to: "n6", label: "readings"),
                     FlowEdge(from: "n2", to: "n7", label: "data"), FlowEdge(from: "n2", to: "n8", label: "sound"),
                     FlowEdge(from: "n7", to: "n9", label: "signal")]
        let card = FlowCard(title: "Phone: main components", nodes: nodes, edges: edges)
        let visible = CGRect(x: 0, y: 25, width: 1440, height: 875)
        // Text room per node at a card width and font scale (FlowBody: chrome padding, then unscaled grid).
        func room(width: CGFloat, scale: CGFloat) -> [String: CGFloat] {
            FlowLayout.layout(nodes: nodes, edges: edges,
                              size: CGSize(width: (width - FlowFit.chromePad) / scale, height: 300))
                .mapValues { $0.width - 12 }
        }
        func cut(_ room: [String: CGFloat]) -> [String] {
            nodes.filter { room[$0.id]! < FlowFit.labelWidth($0.text) }.map(\.text)
        }
        // Before: the tile-shaped width and the plain font cap cut the long labels.
        let before = cut(room(width: 725, scale: CardSize.flowFontScaleCap))
        XCTAssertTrue(before.contains("Power management") && before.contains("Memory and storage")
                      && before.contains("Touchscreen display"), "\(before)")
        let width = FlowFit.enlargedWidth(card, fitted: 725, visible: visible)
        let scale = FlowFit.scale(card, width: width)
        XCTAssertLessThanOrEqual(width, 1152.001)
        XCTAssertEqual(scale, CardSize.flowFontScaleCap, accuracy: 0.001, "room at the full flow font")
        XCTAssertEqual(cut(room(width: width, scale: scale)), [])
        XCTAssertGreaterThanOrEqual(FlowFit.labelWidth("Touchscreen display"), CGFloat("Touchscreen".count) * FlowFit.charWidth)
        // A wider fitted width is kept; the window stays centred inside the screen.
        XCTAssertEqual(FlowFit.enlargedWidth(card, fitted: 1100, visible: visible), 1100, accuracy: 0.001)
        // A row too long for 80 % of the screen lowers the font instead of cutting the words.
        let many = (0..<12).map { FlowNode(id: "m\($0)", text: "Memory and storage") }
        let wide = FlowCard(title: "Many", nodes: many, edges: [])
        let w2 = FlowFit.enlargedWidth(wide, fitted: 725, visible: visible)
        let s2 = FlowFit.scale(wide, width: w2)
        XCTAssertEqual(w2, 1152, accuracy: 0.001)
        XCTAssertLessThan(s2, CardSize.flowFontScaleCap)
        let r2 = FlowLayout.layout(nodes: many, edges: [], size: CGSize(width: (w2 - FlowFit.chromePad) / s2, height: 300))
        XCTAssertTrue(many.allSatisfy { r2[$0.id]!.width - 12 >= FlowFit.labelWidth($0.text) })
    }

    /// Live run 4: "data" and "image and touch" were placed edge to edge and read as one label.
    /// Placed labels keep `gap` clear between them.
    func testFlowEdgeLabelsKeepAGap() {
        let parent = CGRect(x: 0, y: 0, width: 100, height: 30)
        let size = FlowEdgeLabels.size("data", scale: 1)
        let gap: CGFloat = 6
        // The second edge's middle puts its label exactly where the first one ends.
        let firstEnd = parent.midX + gap + size.width
        let childA = CGRect(x: 0, y: 100, width: 100, height: 30)
        let childB = CGRect(x: 2 * (firstEnd - gap) - parent.midX - 50, y: 100, width: 100, height: 30)
        let edges = [(from: parent, to: childA, size: size), (from: parent, to: childB, size: size)]
        let spots = FlowEdgeLabels.place(edges, nodes: [], gap: gap)
        let boxes = zip(edges, spots).compactMap { e, p in
            p.map { CGRect(x: $0.x, y: $0.y - e.size.height / 2, width: e.size.width, height: e.size.height) }
        }
        XCTAssertEqual(boxes.count, 2)
        let a = boxes[0], b = boxes[1]
        let rowsMeet = a.minY < b.maxY && b.minY < a.maxY
        let space = max(b.minX - a.maxX, a.minX - b.maxX)
        XCTAssertTrue(!rowsMeet || space >= gap, "labels \(space) pt apart on one line")
    }
}
