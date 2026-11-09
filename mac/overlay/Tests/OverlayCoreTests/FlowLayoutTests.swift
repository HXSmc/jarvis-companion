import XCTest
@testable import OverlayCore

final class FlowLayoutTests: XCTestCase {
    private func nodes(_ pairs: (String, String)...) -> [FlowNode] {
        pairs.map { FlowNode(id: $0.0, text: $0.1) }
    }

    private func edges(_ pairs: (String, String)...) -> [FlowEdge] {
        pairs.map { FlowEdge(from: $0.0, to: $0.1) }
    }

    private func assertNoOverlaps(_ rects: [String: CGRect], file: StaticString = #filePath, line: UInt = #line) {
        let items = Array(rects)
        for i in 0..<items.count {
            for j in (i + 1)..<items.count {
                XCTAssertFalse(
                    items[i].value.intersects(items[j].value),
                    "\(items[i].key) overlaps \(items[j].key)",
                    file: file, line: line
                )
            }
        }
    }

    func testChainLayersTopToBottom() {
        let flow = nodes(("a", "Wake word"), ("b", "Speech to text"), ("c", "Brain"), ("d", "Overlay"))
        let rects = FlowLayout.layout(
            nodes: flow, edges: edges(("a", "b"), ("b", "c"), ("c", "d")),
            size: CGSize(width: 332, height: 220)
        )
        XCTAssertEqual(rects.count, 4)
        assertNoOverlaps(rects)
        for e in [("a", "b"), ("b", "c"), ("c", "d")] {
            XCTAssertGreaterThan(rects[e.1]!.minY, rects[e.0]!.minY, "edge \(e) must point downward")
        }
        XCTAssertEqual(FlowLayout.depths(nodes: flow, edges: edges(("a", "b"), ("b", "c"), ("c", "d"))), ["a": 0, "b": 1, "c": 2, "d": 3])
    }

    func testCycleTerminatesWithoutOverlaps() {
        let flow = nodes(("a", "Loop"), ("b", "Loop more"), ("c", "Exit"))
        let rects = FlowLayout.layout(
            nodes: flow, edges: edges(("a", "b"), ("b", "a"), ("b", "c")),
            size: CGSize(width: 300, height: 200)
        )
        XCTAssertEqual(rects.count, 3)
        assertNoOverlaps(rects)
    }

    func testDiamondKeepsBothParentsAbove() {
        let flow = nodes(("a", "Start"), ("b", "Left"), ("c", "Right"), ("d", "Join"))
        let rects = FlowLayout.layout(
            nodes: flow, edges: edges(("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")),
            size: CGSize(width: 300, height: 200)
        )
        XCTAssertEqual(rects.count, 4)
        assertNoOverlaps(rects)
        XCTAssertLessThan(rects["a"]!.minY, rects["b"]!.minY)
        XCTAssertEqual(rects["b"]!.minY, rects["c"]!.minY)
        XCTAssertGreaterThan(rects["d"]!.minY, rects["b"]!.maxY)
    }

    func testEmptyInputs() {
        XCTAssertEqual(FlowLayout.layout(nodes: [], edges: [], size: CGSize(width: 100, height: 100)), [:])
        XCTAssertEqual(
            FlowLayout.layout(nodes: nodes(("a", "Solo")), edges: [], size: .zero), [:]
        )
    }
}
