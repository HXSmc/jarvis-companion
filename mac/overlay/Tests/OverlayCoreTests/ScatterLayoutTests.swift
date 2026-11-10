import XCTest
@testable import OverlayCore

// Screen F, visible frame V (menu bar 33 pt, no Dock), its inset area and the orb keep-out.
private let F = CGRect(x: 0, y: 0, width: 1512, height: 982)
private let V = CGRect(x: 0, y: 33, width: 1512, height: 949)
private let inset = ScatterLayout.insetArea(V)
private let orb = ScatterLayout.orbKeepOut(screen: F)
private let sizes = [CGSize(width: 300, height: 120), CGSize(width: 360, height: 200),
                     CGSize(width: 300, height: 90), CGSize(width: 360, height: 150)]

final class ScatterLayoutTests: XCTestCase {

    private func run(count: Int, visible: CGRect, keepOut: [CGRect], size: CGSize? = nil)
            -> (state: ScatterLayout.LayoutState, retired: [String]) {
        var state = ScatterLayout.LayoutState()
        var retired: [String] = []
        for i in 1...count {
            retired += state.cardAdded("c\(i)", size: size ?? sizes[(i - 1) % 4],
                                       visible: visible, keepOut: keepOut)
        }
        return (state, retired)
    }

    private func assertOverlapBounds(_ frames: [CGRect]) {
        for i in frames.indices {
            for j in frames.indices where j > i {
                let overlap = ScatterLayout.overlapArea(frames[i], frames[j])
                XCTAssertLessThanOrEqual(
                    overlap / min(ScatterLayout.area(frames[i]), ScatterLayout.area(frames[j])), 0.30 + 1e-9)
            }
        }
        for frame in frames {
            let covered = frames.reduce(CGFloat(0)) { $0 + ($1 == frame ? 0 : ScatterLayout.overlapArea(frame, $1)) }
            XCTAssertLessThanOrEqual(covered / ScatterLayout.area(frame), 0.60 + 1e-9)
        }
    }

    // MARK: a
    func testSpreadsOverTheWholeScreen() {
        let (state, retired) = run(count: 12, visible: V, keepOut: [orb])
        XCTAssertTrue(retired.isEmpty)
        let frames = state.order.compactMap { state.frames[$0] }
        XCTAssertEqual(frames.count, 12)
        var zones: [Int: Int] = [:]
        var centres: [CGFloat] = []
        for frame in frames {
            XCTAssertTrue(inset.contains(frame))
            let centre = CGPoint(x: frame.midX, y: frame.midY)
            let col = min(2, Int((centre.x - 16) / (1480 / 3)))
            let row = min(2, Int((centre.y - 49) / (917 / 3)))
            zones[row * 3 + col, default: 0] += 1
            centres.append(centre.x)
        }
        XCTAssertGreaterThanOrEqual(zones.count, 7)
        XCTAssertLessThanOrEqual(zones.values.max() ?? 0, 3)
        XCTAssertLessThan(centres.filter { $0 > 1008 }.count, 6)
        XCTAssertGreaterThan((centres.max() ?? 0) - (centres.min() ?? 0), 800)
    }

    // MARK: b
    func testOverlapStaysBounded() {
        let twelve = run(count: 12, visible: V, keepOut: [orb])
        assertOverlapBounds(Array(twelve.state.frames.values))
        let thirty = run(count: 30, visible: V, keepOut: [orb])
        assertOverlapBounds(Array(thirty.state.frames.values))
        XCTAssertFalse(thirty.retired.isEmpty)
        XCTAssertEqual(thirty.state.order.count + thirty.retired.count, 30)
    }

    // MARK: c
    func testNeverOnAKeepOut() {
        let shape = ScatterLayout.shapeKeepOut(CGRect(x: 600, y: 400, width: 300, height: 80), screenWidth: 1512)
        let (state, _) = run(count: 18, visible: V, keepOut: [orb, shape])
        XCTAssertFalse(state.frames.isEmpty)
        for key in state.order {
            guard let frame = state.frames[key] else {
                XCTFail("no frame for placed key \(key)")
                continue
            }
            XCTAssertFalse(frame.intersects(shape), "frame for \(key) intersects the shape keep-out")
            XCTAssertFalse(frame.intersects(orb), "frame for \(key) intersects the orb keep-out")
        }
    }

    // MARK: d
    func testDeterministic() {
        XCTAssertEqual(run(count: 12, visible: V, keepOut: [orb]).state.frames,
                       run(count: 12, visible: V, keepOut: [orb]).state.frames)
        let size = CGSize(width: 300, height: 120)
        let one = ScatterLayout.place(size: size, key: "c1", visible: V, existing: [], keepOut: [orb])
        let two = ScatterLayout.place(size: size, key: "c2", visible: V, existing: [], keepOut: [orb])
        XCTAssertNotEqual(one, two)
        XCTAssertEqual(ScatterLayout.seed("c1"), 0x08a2_7f07_b54a_6859)
    }

    // MARK: e
    func testAddingACardMovesNoOtherCard() {
        var state = run(count: 6, visible: V, keepOut: [orb]).state
        let before = state.frames
        state.cardAdded("c7", size: CGSize(width: 300, height: 120), visible: V, keepOut: [orb])
        for i in 1...6 {
            XCTAssertEqual(state.frames["c\(i)"], before["c\(i)"])
        }
    }

    // MARK: f
    func testFullScreenRetiresBeforeOverlapPassesTheCap() {
        let small = CGRect(x: 0, y: 0, width: 800, height: 600)
        let size = CGSize(width: 360, height: 260)
        var existing: [CGRect] = []
        var retired = false
        var retireKey = ""
        var index = 0
        while !retired && index < 20 {
            index += 1
            let key = "s\(index)"
            switch ScatterLayout.place(size: size, key: key, visible: small, existing: existing, keepOut: []) {
            case .at(let frame):
                existing.append(frame)
                assertOverlapBounds(existing)
            case .retireOldest:
                retired = true
                retireKey = key
            case .noRoom:
                XCTFail("noRoom on a screen with no keep-out")
                return
            }
        }
        XCTAssertTrue(retired, "retire never happened within 20 placements")
        existing.removeFirst()
        let placement = ScatterLayout.place(size: size, key: retireKey, visible: small, existing: existing, keepOut: [])
        guard case .at = placement else {
            XCTFail("expected a placement after retiring the oldest, got \(placement)")
            return
        }
    }

    // MARK: g
    func testReclampKeepsFramesOnScreen() {
        let frames = ["a": CGRect(x: 1400, y: 900, width: 300, height: 120),
                      "b": CGRect(x: 10, y: 10, width: 300, height: 120),
                      "c": CGRect(x: 100, y: 100, width: 300, height: 120)]
        let newV = CGRect(x: 0, y: 25, width: 1280, height: 775)
        let area = ScatterLayout.insetArea(newV)
        let reclamped = ScatterLayout.reclamp(frames: frames, visible: newV)
        for (key, original) in frames {
            guard let moved = reclamped[key] else {
                XCTFail("reclamp lost frame \(key)")
                continue
            }
            XCTAssertTrue(area.contains(moved))
            XCTAssertEqual(moved.width, original.width)
            XCTAssertEqual(moved.height, original.height)
        }
        XCTAssertEqual(ScatterLayout.reclamp(frames: ["c": CGRect(x: 100, y: 100, width: 300, height: 120)],
                                             visible: V)["c"],
                       CGRect(x: 100, y: 100, width: 300, height: 120))
    }

    // MARK: h
    func testOversizeCardIsClampedToTheInsetArea() {
        XCTAssertEqual(ScatterLayout.place(size: CGSize(width: 2000, height: 2000), key: "big",
                                           visible: V, existing: [], keepOut: []),
                       .at(inset))
    }

    // MARK: i
    func testChipFrames() {
        let keys = (0..<6).map { "k\($0)" }
        let frames = ScatterLayout.chipFrames(keys: keys, visible: V, orbOnRight: true)
        for (i, key) in keys.enumerated() {
            XCTAssertEqual(frames[key], CGRect(x: 16, y: 49 + 34 * CGFloat(i), width: 200, height: 28))
        }
        let many = (0..<40).map { "k\($0)" }
        let wrapped = ScatterLayout.chipFrames(keys: many, visible: CGRect(x: 0, y: 0, width: 1512, height: 600),
                                               orbOnRight: true)
        XCTAssertEqual(wrapped["k15"]?.minX, 16)
        XCTAssertEqual(wrapped["k16"], CGRect(x: 222, y: 16, width: 200, height: 28))
        let left = ScatterLayout.chipFrames(keys: ["k0"], visible: V, orbOnRight: false)
        XCTAssertEqual(left["k0"]?.minX, 1512 - 16 - 200)
    }

    // MARK: j
    func testLayoutStateAsideAndRestore() {
        var state = run(count: 4, visible: V, keepOut: [orb]).state
        let saved = state.frames
        state.aside()
        XCTAssertEqual(state.mode, .aside)
        XCTAssertEqual(state.chipKeys, state.order)
        let afterAside = state
        state.aside()
        XCTAssertEqual(state, afterAside)
        state.cardAdded("c5", size: CGSize(width: 300, height: 120), visible: V, keepOut: [orb])
        XCTAssertNotNil(state.frames["c5"])
        XCTAssertTrue(state.chipKeys.contains("c5"))
        for i in 1...4 {
            XCTAssertEqual(state.frames["c\(i)"], saved["c\(i)"])
        }
        state.restore()
        XCTAssertEqual(state.mode, .normal)
        XCTAssertTrue(state.chipKeys.isEmpty)
        for i in 1...4 {
            XCTAssertEqual(state.frames["c\(i)"], saved["c\(i)"])
        }
        let afterRestore = state
        state.restore()
        XCTAssertEqual(state, afterRestore)
        state.aside()
        state.clear()
        XCTAssertEqual(state.mode, .normal)
        XCTAssertTrue(state.frames.isEmpty)
        XCTAssertTrue(state.order.isEmpty)

        // While aside nothing is retired, even past the overlap caps on a small screen.
        let small = CGRect(x: 0, y: 0, width: 800, height: 600)
        let size = CGSize(width: 360, height: 260)
        var parked = ScatterLayout.LayoutState()
        for i in 1...4 {
            parked.cardAdded("c\(i)", size: size, visible: small, keepOut: [])
        }
        parked.aside()
        var retired: [String] = []
        for i in 5...10 {
            retired += parked.cardAdded("c\(i)", size: size, visible: small, keepOut: [])
        }
        XCTAssertTrue(retired.isEmpty)
        XCTAssertEqual(parked.order.count, 10)
    }

    // MARK: k
    func testDebugCommandParse() {
        XCTAssertEqual(LayoutDebugCommand.parse("aside"), .aside)
        XCTAssertEqual(LayoutDebugCommand.parse("restore"), .restore)
        XCTAssertEqual(LayoutDebugCommand.parse("clear"), .clear)
        XCTAssertEqual(LayoutDebugCommand.parse("shape"), .shape)
        XCTAssertEqual(LayoutDebugCommand.parse("demo:1"), .demo(1))
        XCTAssertEqual(LayoutDebugCommand.parse("demo:18"), .demo(18))
        for bad in ["demo:0", "demo:19", "demo:x", "ASIDE ", "", "aside;clear", "demo:", "demo:-1"] {
            XCTAssertNil(LayoutDebugCommand.parse(bad), "expected nil for \(bad)")
        }
        XCTAssertNil(LayoutDebugCommand.parse(nil))

        let cards = LayoutDebugCommand.demoCards(count: 14)
        XCTAssertEqual(cards.count, 14)
        XCTAssertEqual(cards.map(\.id), (1...14).map { Optional("demo-\($0)") })
        XCTAssertGreaterThanOrEqual(Set(cards.map(\.typeName)).count, 4)
        for case .link(let link) in cards {
            XCTAssertEqual(link.url, "https://example.com")
        }
        XCTAssertTrue(LayoutDebugCommand.demoCards(count: 0).isEmpty)
    }
}
