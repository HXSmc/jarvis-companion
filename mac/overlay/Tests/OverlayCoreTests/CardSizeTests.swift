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
}
