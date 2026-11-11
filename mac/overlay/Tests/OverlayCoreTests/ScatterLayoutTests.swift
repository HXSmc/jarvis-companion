import XCTest
@testable import OverlayCore

// Screen F, visible frame V (menu bar 33 pt, no Dock), its inset area and the orb keep-out.
private let F = CGRect(x: 0, y: 0, width: 1512, height: 982)
private let V = CGRect(x: 0, y: 33, width: 1512, height: 949)
private let inset = ScatterLayout.insetArea(V)
private let orb = ScatterLayout.orbKeepOut(screen: F)
private let sizes = [CGSize(width: 300, height: 120), CGSize(width: 360, height: 200),
                     CGSize(width: 300, height: 90), CGSize(width: 360, height: 150)]
/// Demo card sizes per kind (info, stat, bar, link, flow), as CardView measures them; the live
/// frames of the Mac test show the same sizes to within 1 pt.
private let kindSizes = [CGSize(width: 300, height: 93), CGSize(width: 300, height: 127), CGSize(width: 360, height: 136),
                         CGSize(width: 300, height: 127), CGSize(width: 360, height: 204)]
// The Mac of the live test: 1440x900 pt, menu bar 30 pt, Dock 74 pt. With these numbers the
// engine gives the live demo:14 and demo:18 frames to within 1 pt.
private let liveScreen = CGRect(x: 0, y: 0, width: 1440, height: 900)
private let liveVisible = CGRect(x: 0, y: 30, width: 1440, height: 796)

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
        // Retiring oldest first always ends in a placement, at the latest on an empty screen.
        var placement = ScatterLayout.Placement.retireOldest
        while placement == .retireOldest, !existing.isEmpty {
            existing.removeFirst()
            placement = ScatterLayout.place(size: size, key: retireKey, visible: small, existing: existing, keepOut: [])
        }
        guard case .at(let frame) = placement else {
            XCTFail("expected a placement after retiring the oldest, got \(placement)")
            return
        }
        assertOverlapBounds(existing + [frame])
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
            XCTAssertEqual(frames[key], CGRect(x: 16, y: 153 + 34 * CGFloat(i), width: 200, height: 28))
        }
        let many = (0..<40).map { "k\($0)" }
        let wrapped = ScatterLayout.chipFrames(keys: many, visible: CGRect(x: 0, y: 0, width: 1512, height: 600),
                                               orbOnRight: true)
        XCTAssertEqual(wrapped["k12"]?.minX, 16)
        XCTAssertEqual(wrapped["k13"], CGRect(x: 222, y: 120, width: 200, height: 28))
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

        // While aside nothing is retired and the caps still hold: a card with no spot waits
        // unplaced, and after restore it is placed by the normal rule (oldest retires).
        let small = CGRect(x: 0, y: 0, width: 800, height: 600)
        let size = CGSize(width: 360, height: 260)
        var parked = ScatterLayout.LayoutState()
        for i in 1...4 {
            parked.cardAdded("c\(i)", size: size, visible: small, keepOut: [])
        }
        let placedBefore = parked.order
        parked.aside()
        var retired: [String] = []
        for i in 5...10 {
            retired += parked.cardAdded("c\(i)", size: size, visible: small, keepOut: [])
        }
        XCTAssertTrue(retired.isEmpty)
        XCTAssertEqual(Array(parked.order.prefix(placedBefore.count)), placedBefore)
        XCTAssertNil(parked.frames["c10"])
        // A card with no spot yet still shows as a chip while aside.
        XCTAssertTrue(parked.chipKeys.contains("c10"))
        assertOverlapBounds(parked.order.compactMap { parked.frames[$0] })
        parked.restore()
        XCTAssertFalse(parked.cardAdded("c10", size: size, visible: small, keepOut: []).isEmpty)
        XCTAssertNotNil(parked.frames["c10"])
        XCTAssertFalse(parked.waiting.contains("c10"))
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

    // MARK: idle deadline (hub amendment A1.1)
    func testIdleDeadlineWaitsOnlyWhileThePointerRestsOnAPanel() {
        let cards = [CGRect(x: 100, y: 100, width: 300, height: 120), CGRect(x: 700, y: 400, width: 200, height: 28)]
        XCTAssertTrue(ScatterLayout.pointerRests(CGPoint(x: 150, y: 150), onAnyOf: cards))
        XCTAssertTrue(ScatterLayout.pointerRests(CGPoint(x: 750, y: 410), onAnyOf: cards))
        XCTAssertFalse(ScatterLayout.pointerRests(CGPoint(x: 50, y: 50), onAnyOf: cards))
        // The hovered card was replaced or removed: its frame is gone, so the deadline clears.
        XCTAssertFalse(ScatterLayout.pointerRests(CGPoint(x: 150, y: 150), onAnyOf: Array(cards.dropFirst())))
        XCTAssertFalse(ScatterLayout.pointerRests(CGPoint(x: 150, y: 150), onAnyOf: []))
    }

    // MARK: round 1 (hub amendment A2)

    /// Demo cards with fixed per-kind sizes (info, stat, bar, link, flow), as CardView measures them.
    private func demoRun(_ count: Int, visible: CGRect = V, keepOut: [CGRect] = [orb]) -> ScatterLayout.LayoutState {
        var state = ScatterLayout.LayoutState()
        for (index, card) in LayoutDebugCommand.demoCards(count: count).enumerated() {
            state.cardAdded(card.listKey, size: kindSizes[index % 5], visible: visible, keepOut: keepOut)
        }
        return state
    }

    private func overlapRatios(_ frames: [CGRect]) -> [CGFloat] {
        var ratios: [CGFloat] = []
        for i in frames.indices {
            for j in frames.indices where j > i {
                let overlap = ScatterLayout.overlapArea(frames[i], frames[j])
                if overlap > 0 { ratios.append(overlap / min(ScatterLayout.area(frames[i]), ScatterLayout.area(frames[j]))) }
            }
        }
        return ratios
    }

    func testFourteenDemoCardsAreLayered() {
        let state = demoRun(14)
        XCTAssertEqual(state.order.count, 14)
        let frames = state.order.compactMap { state.frames[$0] }
        let ratios = overlapRatios(frames)
        XCTAssertTrue((3...6).contains(ratios.count), "overlapping pairs: \(ratios)")
        for ratio in ratios { XCTAssertTrue((0.08...0.30).contains(ratio), "pair overlap \(ratio)") }
        assertOverlapBounds(frames)
        XCTAssertEqual(demoRun(14).frames, state.frames)
    }

    func testFirstSixCardsDoNotOverlap() {
        let state = demoRun(6)
        XCTAssertTrue(overlapRatios(state.order.compactMap { state.frames[$0] }).isEmpty)
    }

    func testCardsOverlapOrKeepATwentyPointGap() {
        for count in [6, 14, 18] {
            let state = demoRun(count)
            let frames = state.order.compactMap { state.frames[$0] }
            for i in frames.indices {
                for j in frames.indices where j > i {
                    let overlap = ScatterLayout.overlapArea(frames[i], frames[j])
                    if overlap > 0 {
                        XCTAssertGreaterThanOrEqual(
                            overlap / min(ScatterLayout.area(frames[i]), ScatterLayout.area(frames[j])), 0.08 - 1e-9)
                    } else {
                        XCTAssertFalse(frames[i].intersects(frames[j].insetBy(dx: -20, dy: -20)),
                                       "\(count) cards: pair \(i),\(j) closer than 20 pt")
                    }
                }
            }
            assertOverlapBounds(frames)
        }
    }

    func testEveryZoneFillsBeforeASecondCard() {
        let area = inset
        func zone(_ frame: CGRect) -> Int {
            let col = min(2, Int((frame.midX - area.minX) / (area.width / 3)))
            let row = min(2, Int((frame.midY - area.minY) / (area.height / 3)))
            return row * 3 + col
        }
        var state = ScatterLayout.LayoutState()
        for (index, card) in LayoutDebugCommand.demoCards(count: 9).enumerated() {
            var occupied = [Int](repeating: 0, count: 9)
            for frame in state.frames.values { occupied[zone(frame)] += 1 }
            let layered = ScatterLayout.prefersLayer(key: card.listKey, existingCount: state.order.count)
            state.cardAdded(card.listKey, size: kindSizes[index % 5], visible: V, keepOut: [orb])
            guard let frame = state.frames[card.listKey] else { return XCTFail("no frame for \(card.listKey)") }
            // A layered card sits on an older one on purpose; every other card takes a least-used zone.
            if !layered { XCTAssertEqual(occupied[zone(frame)], occupied.min(), "\(card.listKey) skipped an emptier zone") }
        }
        let zones = Set(state.frames.values.map(zone))
        XCTAssertGreaterThanOrEqual(zones.count, 8)
    }

    func testNoRoomWhenOnlyAKeepOutIsInTheWay() {
        let size = CGSize(width: 300, height: 120)
        XCTAssertEqual(ScatterLayout.place(size: size, key: "c1", visible: V, existing: [], keepOut: [V]), .noRoom)
        XCTAssertEqual(ScatterLayout.place(size: size, key: "c2", visible: V,
                                           existing: [CGRect(x: 100, y: 100, width: 300, height: 120)], keepOut: [V]),
                       .noRoom)
        var state = ScatterLayout.LayoutState()
        XCTAssertTrue(state.cardAdded("c3", size: size, visible: V, keepOut: [V]).isEmpty)
        XCTAssertNil(state.frames["c3"])
        XCTAssertTrue(state.order.isEmpty)
    }

    // MARK: round 2 (live test of 23b6b39)

    /// Every pair keeps a 20 pt gap or overlaps 8-30 % of the smaller card on one corner (20 pt
    /// to 75 % of the smaller card deep on each axis, the older card's top-left corner left
    /// uncovered); no card touches a keep-out. Returns the overlapping pairs and the largest share.
    @discardableResult
    private func assertLayered(_ state: ScatterLayout.LayoutState, keepOut: [CGRect], _ label: String,
                               line: UInt = #line) -> (pairs: Int, maxRatio: CGFloat) {
        let frames = state.order.compactMap { state.frames[$0] }
        XCTAssertEqual(frames.count, state.order.count, label, line: line)
        var pairs = 0
        var maxRatio: CGFloat = 0
        for i in frames.indices {
            for keep in keepOut {
                XCTAssertFalse(frames[i].intersects(keep), "\(label): card \(i) on a keep-out", line: line)
            }
            for j in frames.indices where j > i {
                let (below, top) = (frames[i], frames[j])
                let overlap = ScatterLayout.overlapArea(below, top)
                guard overlap > 0 else {
                    XCTAssertFalse(below.intersects(top.insetBy(dx: -20, dy: -20)),
                                   "\(label): pair \(i),\(j) closer than 20 pt", line: line)
                    continue
                }
                let ratio = overlap / min(ScatterLayout.area(below), ScatterLayout.area(top))
                let shared = below.intersection(top)
                pairs += 1
                maxRatio = max(maxRatio, ratio)
                XCTAssertTrue(ratio >= 0.08 - 1e-9 && ratio <= 0.30 + 1e-9,
                              "\(label): pair \(i),\(j) overlap \(ratio)", line: line)
                XCTAssertTrue(shared.width >= 20 && shared.width <= 0.75 * min(below.width, top.width) + 1e-9
                              && shared.height >= 20 && shared.height <= 0.75 * min(below.height, top.height) + 1e-9,
                              "\(label): pair \(i),\(j) overlap \(shared.size) runs along a side", line: line)
                XCTAssertTrue(shared.minX > below.minX || shared.minY > below.minY,
                              "\(label): pair \(i),\(j) covers the title corner", line: line)
            }
        }
        assertOverlapBounds(frames)
        return (pairs, maxRatio)
    }

    /// The live test: demo:14 and demo:18 on the 1440x900 Mac with the measured card sizes.
    /// Before this round the engine itself put full-side strips there (note 11 / link 14: a
    /// 29x93 pt strip, about 10 %, read as two cards 1-2 px apart).
    func testDemoCardsOnTheLiveScreen() {
        let keepOut = [ScatterLayout.orbKeepOut(screen: liveScreen)]
        for count in [14, 18] {
            let state = demoRun(count, visible: liveVisible, keepOut: keepOut)
            let (pairs, maxRatio) = assertLayered(state, keepOut: keepOut, "live demo:\(count)")
            print(String(format: "live 1440x900 demo:%d: %d shown, %d overlapping pairs, max overlap %.3f",
                         count, state.order.count, pairs, Double(maxRatio)))
        }
        XCTAssertEqual(demoRun(14, visible: liveVisible, keepOut: keepOut).order.count, 14)
        for count in [14, 18] {
            assertLayered(demoRun(count), keepOut: [orb], "1512 demo:\(count)")
        }
    }

    /// A card that grows after placement (60 pt wider and taller, as when its content is
    /// replaced) keeps its spot only while every rule still holds; otherwise it alone is
    /// placed again (or the oldest retires). No other card ever moves.
    func testCardGrowingAfterPlacementKeepsTheRules() {
        for (screen, visible) in [(F, V), (liveScreen, liveVisible)] {
            let keepOut = [ScatterLayout.orbKeepOut(screen: screen)]
            for count in [14, 18] {
                var state = demoRun(count, visible: visible, keepOut: keepOut)
                var replaced = 0
                for key in state.order {
                    guard let old = state.frames[key] else { continue }
                    let others = state.frames.filter { $0.key != key }
                    let grown = CGSize(width: old.width + 60, height: old.height + 60)
                    let retired = state.cardResized(key, size: grown, visible: visible, keepOut: keepOut)
                    for (other, frame) in others where !retired.contains(other) {
                        XCTAssertEqual(state.frames[other], frame, "\(other) moved when \(key) grew")
                    }
                    if state.frames[key] != ScatterLayout.clamp(CGRect(origin: old.origin, size: grown),
                                                                into: ScatterLayout.insetArea(visible)) {
                        replaced += 1
                    }
                    assertLayered(state, keepOut: keepOut, "\(Int(screen.width)) demo:\(count), \(key) grown")
                }
                XCTAssertGreaterThan(replaced, 0, "growth never broke a rule, so the test proves nothing")
                let (pairs, maxRatio) = assertLayered(state, keepOut: keepOut, "grown")
                print(String(format: "%.0f demo:%d grown by 60 pt: %d shown, %d re-placed, %d overlapping pairs, max overlap %.3f",
                             Double(screen.width), count, state.order.count, replaced, pairs, Double(maxRatio)))
            }
        }

        // Room to grow: the card keeps its spot (only clamped).
        var state = ScatterLayout.LayoutState()
        state.cardAdded("a", size: CGSize(width: 300, height: 120), visible: V, keepOut: [orb])
        guard let old = state.frames["a"] else { return XCTFail("no frame for a") }
        let grown = CGSize(width: 340, height: 160)
        XCTAssertTrue(state.cardResized("a", size: grown, visible: V, keepOut: [orb]).isEmpty)
        XCTAssertEqual(state.frames["a"], ScatterLayout.clamp(CGRect(origin: old.origin, size: grown), into: inset))
    }

    // MARK: round 3 (code lens finding, hub amendment A3)

    /// A card replaced by id with the same size never moves, even with newer cards lying on its
    /// lower-right corner (demo-6 of demo:14 and demo-6 / demo-10 of demo:18 used to jump).
    func testUnchangedSizeReplaceNeverMovesACard() {
        for (screen, visible) in [(F, V), (liveScreen, liveVisible)] {
            let keepOut = [ScatterLayout.orbKeepOut(screen: screen)]
            for count in [14, 18] {
                var state = demoRun(count, visible: visible, keepOut: keepOut)
                let before = state
                for key in before.order {
                    guard let frame = before.frames[key] else { continue }
                    XCTAssertTrue(state.cardResized(key, size: frame.size, visible: visible, keepOut: keepOut).isEmpty)
                }
                XCTAssertEqual(state, before, "\(Int(screen.width)) demo:\(count): a same-size replace moved a card")
            }
        }
    }

    /// A newer card on the lower-right corner of a resized card lies on top of it, so the corner
    /// rule is checked with the newer card over it, not the other way round.
    func testResizeChecksNewerCardsWithTheCornerRuleReversed() {
        let resized = CGRect(x: 100, y: 100, width: 300, height: 204)
        let newer = CGRect(x: 300, y: 250, width: 300, height: 200)
        let covered = ScatterLayout.coverage([newer])
        XCTAssertNotNil(ScatterLayout.fitness(resized, existing: [newer], covered: covered, newerFrom: 0))
        XCTAssertNil(ScatterLayout.fitness(resized, existing: [newer], covered: covered))
    }

    func testDragThreshold() {
        let start = CGPoint(x: 100, y: 100)
        XCTAssertFalse(ScatterLayout.isDrag(from: start, to: start))
        XCTAssertFalse(ScatterLayout.isDrag(from: start, to: CGPoint(x: 104, y: 100)))
        XCTAssertFalse(ScatterLayout.isDrag(from: start, to: CGPoint(x: 102, y: 103)))
        XCTAssertTrue(ScatterLayout.isDrag(from: start, to: CGPoint(x: 103, y: 104)))
        XCTAssertTrue(ScatterLayout.isDrag(from: start, to: CGPoint(x: 100, y: 95)))
    }

    /// A dragged card is pinned where it is dropped (clamped fully inside the visible frame, not
    /// the inset area), comes to the front, and keeps its frame through a resize, a reclamp
    /// that leaves it on screen, and aside -> restore (frames and z-order unchanged).
    func testDraggedCardIsPinned() {
        var state = demoRun(6)
        guard let old = state.frames["demo-2"] else { return XCTFail("no frame for demo-2") }
        let others = state.frames.filter { $0.key != "demo-2" }
        state.cardDragged("demo-2", to: CGRect(origin: CGPoint(x: 700, y: 500), size: old.size), visible: V)
        XCTAssertEqual(state.frames["demo-2"], CGRect(origin: CGPoint(x: 700, y: 500), size: old.size))
        XCTAssertEqual(state.order.last, "demo-2")
        XCTAssertEqual(state.pinned, ["demo-2"])
        for (key, frame) in others { XCTAssertEqual(state.frames[key], frame, "\(key) moved on a drag") }

        // Dropped past the bottom-left: clamped onto the visible frame's edge, inside it.
        state.cardDragged("demo-2", to: CGRect(origin: CGPoint(x: -80, y: 2000), size: old.size), visible: V)
        XCTAssertEqual(state.frames["demo-2"], CGRect(x: V.minX, y: V.maxY - old.height, width: old.width, height: old.height))
        let edge = state.frames["demo-2"]

        // Reclamp on the same screen keeps it (it is inside the visible frame, though not the inset area).
        state.reclamp(visible: V)
        XCTAssertEqual(state.frames["demo-2"], edge)
        // A resize keeps the origin; a card that would leave the screen only clamps.
        XCTAssertTrue(state.cardResized("demo-2", size: CGSize(width: old.width + 60, height: old.height + 60),
                                        visible: V, keepOut: [orb]).isEmpty)
        XCTAssertEqual(state.frames["demo-2"],
                       CGRect(x: V.minX, y: V.maxY - old.height - 60, width: old.width + 60, height: old.height + 60))
        XCTAssertEqual(state.order.last, "demo-2")

        // A smaller screen: only clamped as far as needed to stay visible.
        let small = CGRect(x: 0, y: 25, width: 1280, height: 775)
        state.reclamp(visible: small)
        guard let reclamped = state.frames["demo-2"] else { return XCTFail("pinned card lost on reclamp") }
        XCTAssertTrue(small.contains(reclamped))
        XCTAssertEqual(reclamped.maxY, small.maxY)

        let saved = state
        state.aside()
        state.restore()
        XCTAssertEqual(state.frames, saved.frames)
        XCTAssertEqual(state.order, saved.order)
        XCTAssertEqual(state.pinned, saved.pinned)
        state.clear()
        XCTAssertTrue(state.pinned.isEmpty)
    }

    /// New cards treat a pinned card as occupied: each keeps a 20 pt gap to it or lies over one
    /// of its corners within the caps.
    func testNewCardsAreLaidOutAroundAPinnedCard() {
        var state = demoRun(4)
        guard let old = state.frames["demo-1"] else { return XCTFail("no frame for demo-1") }
        let pinnedFrame = CGRect(origin: CGPoint(x: 600, y: 380), size: old.size)
        state.cardDragged("demo-1", to: pinnedFrame, visible: V)
        for (index, card) in LayoutDebugCommand.demoCards(count: 14).enumerated().dropFirst(4) {
            state.cardAdded(card.listKey, size: kindSizes[index % 5], visible: V, keepOut: [orb])
            XCTAssertEqual(state.frames["demo-1"], pinnedFrame, "pinned card moved when \(card.listKey) came")
            guard let frame = state.frames[card.listKey] else { continue }
            let overlap = ScatterLayout.overlapArea(frame, pinnedFrame)
            if overlap == 0 {
                XCTAssertFalse(frame.intersects(pinnedFrame.insetBy(dx: -20, dy: -20)), "\(card.listKey) too close")
            } else {
                let ratio = overlap / min(ScatterLayout.area(frame), ScatterLayout.area(pinnedFrame))
                XCTAssertTrue((0.08...0.30).contains(ratio), "\(card.listKey) overlaps the pinned card by \(ratio)")
                XCTAssertTrue(ScatterLayout.isCornerOverlap(frame, over: pinnedFrame), "\(card.listKey) off a corner")
            }
        }
    }

    /// Room is made by retiring unpinned cards first, even when the pinned card is the oldest.
    func testPinnedCardRetiresLast() {
        let small = CGRect(x: 0, y: 0, width: 800, height: 600)
        let size = CGSize(width: 360, height: 260)
        var state = ScatterLayout.LayoutState()
        state.cardAdded("c1", size: size, visible: small, keepOut: [])
        // Dropped in the top-left corner: the rest of the screen always has room for one more card.
        state.cardDragged("c1", to: CGRect(x: -50, y: -50, width: 360, height: 260), visible: small)
        let corner = CGRect(x: 0, y: 0, width: 360, height: 260)
        XCTAssertEqual(state.frames["c1"], corner)
        var retired: [String] = []
        for i in 2...12 {
            retired += state.cardAdded("c\(i)", size: size, visible: small, keepOut: [])
            XCTAssertEqual(state.retireOrder.last, "c1")
        }
        XCTAssertFalse(retired.isEmpty, "the small screen never filled up")
        XCTAssertFalse(retired.contains("c1"), "the pinned card retired while unpinned cards were left")
        XCTAssertEqual(state.frames["c1"], corner)
    }

    func testNewLineBandMovesUnpinnedCardsOffItOnce() throws {
        var state = ScatterLayout.LayoutState()
        state.cardAdded("history", size: sizes[0], visible: V, keepOut: [orb])
        state.cardAdded("pinned", size: sizes[2], visible: V, keepOut: [orb])
        let history = try XCTUnwrap(state.frames["history"])
        let pinnedFrame = try XCTUnwrap(state.frames["pinned"])
        state.cardDragged("pinned", to: pinnedFrame, visible: V)
        // The band arrives later, right under the history card (and the pinned one).
        let band = ScatterLayout.shapeKeepOut(history.union(pinnedFrame).insetBy(dx: 10, dy: 10), screenWidth: F.width)
        let retired = state.moveOff([band], visible: V, keepOut: [orb, band])
        XCTAssertEqual(retired, [])
        let moved = try XCTUnwrap(state.frames["history"])
        XCTAssertFalse(moved.intersects(band))
        XCTAssertEqual(state.frames["pinned"], pinnedFrame)  // the user's spot stays
        // A band nobody covers moves nothing.
        let before = state
        state.moveOff([CGRect(x: -500, y: -500, width: 10, height: 10)], visible: V, keepOut: [orb])
        XCTAssertEqual(state, before)
    }

    func testCardWithNoOtherSpotStaysPut() throws {
        var state = ScatterLayout.LayoutState()
        state.cardAdded("a", size: sizes[0], visible: V, keepOut: [orb])
        let frame = try XCTUnwrap(state.frames["a"])
        let everywhere = V.insetBy(dx: -50, dy: -50)
        state.moveOff([everywhere], visible: V, keepOut: [everywhere])
        XCTAssertEqual(state.frames["a"], frame)
        XCTAssertTrue(state.waiting.isEmpty)
    }

    func testAsideMovesNothing() {
        var state = ScatterLayout.LayoutState()
        state.cardAdded("a", size: sizes[0], visible: V, keepOut: [orb])
        state.aside()
        let before = state
        state.moveOff([V], visible: V, keepOut: [V])
        XCTAssertEqual(state, before)
    }
}
