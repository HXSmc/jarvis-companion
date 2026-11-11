import XCTest
@testable import OverlayCore

/// f52-cardfix. Top-left points, y down.
/// Laptop: 1440x900 screen, 25 pt menu bar, no Dock -> inset area x 16-1424, y 41-884,
/// notification zone x 1060-1440 y 25-155, orb keep-out x 1348-1464 y 768-924 (`orbKeepOut` takes
/// the full screen rect, `notificationZone` the visible frame).
/// Ultrawide: 3440x1440 screen -> inset area x 16-3424, y 41-1424, zone x 3060-3440 y 25-155.
private struct Screen {
    let name: String
    let screen: CGRect
    let visible: CGRect
    var area: CGRect { ScatterLayout.insetArea(visible) }
    var reserved: [CGRect] { [ScatterLayout.orbKeepOut(screen: screen), ScatterLayout.notificationZone(visible: visible)] }
    /// The permission card's spot (top right), as OverlayPanel reports it.
    var permission: CGRect { CGRect(x: visible.maxX - 16 - 320, y: visible.minY + 16, width: 320, height: 120) }
}

private let laptop = Screen(name: "laptop", screen: CGRect(x: 0, y: 0, width: 1440, height: 900),
                            visible: CGRect(x: 0, y: 25, width: 1440, height: 875))
private let ultrawide = Screen(name: "ultrawide", screen: CGRect(x: 0, y: 0, width: 3440, height: 1440),
                               visible: CGRect(x: 0, y: 25, width: 3440, height: 1415))
private let card = CGSize(width: 320, height: 160)
/// Mixed sizes, including the 340x360 flowchart card.
private let mixed = [CGSize(width: 320, height: 160), CGSize(width: 340, height: 360), CGSize(width: 260, height: 120),
                     CGSize(width: 300, height: 200), CGSize(width: 220, height: 90), CGSize(width: 320, height: 160)]
private let roomy = CGRect(x: 0, y: 0, width: 20000, height: 20000)
private let moves: [LayoutMode] = [.left, .right, .up, .down, .topLeft, .topRight, .bottomLeft, .bottomRight, .otherSide]

final class CardFixTests: XCTestCase {

    // MARK: helpers

    /// n cards on a diagonal across the whole width (too wide for any shift): card k is k-th in
    /// reading order (y grows by 40 per card).
    private func diagonal(_ count: Int, sizes: [CGSize] = [card], on screen: Screen) -> [CGRect] {
        let span = screen.area.width - 340
        return (0..<count).map { index in
            CGRect(origin: CGPoint(x: screen.area.minX + (count == 1 ? 0 : CGFloat(index) * span / CGFloat(count - 1)),
                                   y: 100 + CGFloat(index) * 40), size: sizes[index % sizes.count])
        }
    }

    /// n cards at seeded random spots inside the inset area (overlaps allowed, like hand drags).
    private func scattered(_ count: Int, sizes: [CGSize], on screen: Screen, seed: String) -> [CGRect] {
        var rng = ScatterLayout.SplitMix64(state: ScatterLayout.seed(seed))
        let area = screen.area
        return (0..<count).map { index in
            let size = sizes[index % sizes.count]
            return CGRect(x: area.minX + rng.unit() * (area.width - size.width),
                          y: area.minY + rng.unit() * (area.height - size.height), width: size.width, height: size.height)
        }
    }

    private func state(_ frames: [CGRect], visible: CGRect) -> ScatterLayout.LayoutState {
        var state = ScatterLayout.LayoutState()
        for (index, frame) in frames.enumerated() {
            state.cardAdded("k\(index)", size: frame.size, visible: roomy, keepOut: [])
            state.cardDragged("k\(index)", to: frame, visible: visible)
        }
        return state
    }

    private func readingOrder(_ frames: [CGRect]) -> [Int] {
        frames.indices.sorted { (frames[$0].minY, frames[$0].minX, $0) < (frames[$1].minY, frames[$1].minX, $1) }
    }

    private func points(_ pairs: [(CGFloat, CGFloat)]) -> [CGPoint] { pairs.map { CGPoint(x: $0.0, y: $0.1) } }

    /// Every start the properties run over: both screens, with and without keep-outs (orb, zone,
    /// permission card), uniform and mixed sizes, a diagonal and a scattered start, 1-12 cards.
    private func forEachStart(_ body: (_ old: [CGRect], _ screen: Screen, _ keepOut: [CGRect], _ label: String) -> Void) {
        for screen in [laptop, ultrawide] {
            for keepOut in [[], screen.reserved + [screen.permission]] {
                for sizes in [[card], mixed] {
                    for count in 1...12 {
                        let starts = [("diagonal", diagonal(count, sizes: sizes, on: screen)),
                                      ("scattered", scattered(count, sizes: sizes, on: screen, seed: "\(screen.name)-\(count)"))]
                        for (start, old) in starts {
                            body(old, screen, keepOut, "\(screen.name) \(start) \(count) cards, \(sizes.count) sizes, \(keepOut.count) keep-outs")
                        }
                    }
                }
            }
        }
    }

    // MARK: thing 1, criterion 1: a repeated move changes nothing

    /// f = reflow toward `direction`; f(f(x)) == f(x) within 0.5 pt per frame. otherSide swaps
    /// sides by design (a group on the left goes right, then left again), so for it: f(f(x)) sits in
    /// the other half when f(x) fits one, and without keep-outs f(f(f(x))) == f(x). With keep-outs
    /// the two sides are not mirror images (8 cards fit the left half but not the right half beside
    /// the zone; a mirror shifted off the zone straddles the middle), so no cycle is promised there.
    private func assertMoveTwiceEqualsMoveOnce(_ direction: LayoutMode, line: UInt = #line) {
        var checked = 0
        forEachStart { old, screen, keepOut, label in
            let move = { (frames: [CGRect]) in
                ScatterLayout.reflow(old: frames, direction: direction, visible: screen.visible, keepOut: keepOut)?.frames
            }
            guard let once = move(old) else { return }
            guard let twice = move(once) else { return XCTFail("second move refused: \(direction) \(label)", line: line) }
            checked += 1
            if direction != .otherSide {
                XCTAssertTrue(ScatterLayout.sameFrames(twice, once), "moved again: \(direction) \(label)", line: line)
                return
            }
            let box = { (frames: [CGRect]) in frames.reduce(CGRect.null) { $0.union($1) } }
            let mid = screen.visible.midX
            if box(once).maxX <= mid + 0.5 || box(once).minX >= mid - 0.5 {
                XCTAssertNotEqual(box(twice).midX <= mid, box(once).midX <= mid, "other side stayed: \(label)", line: line)
            }
            guard keepOut.isEmpty else { return }
            guard let thrice = move(twice) else { return XCTFail("third move refused: \(label)", line: line) }
            XCTAssertTrue(ScatterLayout.sameFrames(thrice, once), "other side twice is not back: \(label)", line: line)
        }
        XCTAssertGreaterThan(checked, 0, line: line)
    }

    func testPropertyMoveTwiceEqualsMoveOnceLeft() { assertMoveTwiceEqualsMoveOnce(.left) }
    func testPropertyMoveTwiceEqualsMoveOnceRight() { assertMoveTwiceEqualsMoveOnce(.right) }
    func testPropertyMoveTwiceEqualsMoveOnceUp() { assertMoveTwiceEqualsMoveOnce(.up) }
    func testPropertyMoveTwiceEqualsMoveOnceDown() { assertMoveTwiceEqualsMoveOnce(.down) }
    func testPropertyMoveTwiceEqualsMoveOnceTopLeft() { assertMoveTwiceEqualsMoveOnce(.topLeft) }
    func testPropertyMoveTwiceEqualsMoveOnceTopRight() { assertMoveTwiceEqualsMoveOnce(.topRight) }
    func testPropertyMoveTwiceEqualsMoveOnceBottomLeft() { assertMoveTwiceEqualsMoveOnce(.bottomLeft) }
    func testPropertyMoveTwiceEqualsMoveOnceBottomRight() { assertMoveTwiceEqualsMoveOnce(.bottomRight) }
    func testPropertyMoveTwiceEqualsMoveOnceOtherSide() { assertMoveTwiceEqualsMoveOnce(.otherSide) }

    // MARK: thing 1, criterion 2: the live swap (mac-movefix case 13)

    /// 4 cards, laptop, "top right corner" twice. Before f52-cardfix the second move re-sorted the
    /// row at y 221 by x and swapped cards 2 and 3 (764 <-> 1104).
    func testRepeatedTopRightFourCardsSwapsNothing() {
        var moved = state(diagonal(4, on: laptop), visible: laptop.visible)
        XCTAssertTrue(moved.moveAll(.topRight, visible: laptop.visible, keepOut: laptop.reserved))
        XCTAssertEqual((0..<4).map { moved.frames["k\($0)"]!.origin }, points([(720, 41), (764, 221), (1104, 221), (1104, 401)]))
        let first = moved
        XCTAssertTrue(moved.moveAll(.topRight, visible: laptop.visible, keepOut: laptop.reserved))
        XCTAssertEqual(moved.frames, first.frames)
        XCTAssertEqual(moved.order, first.order)
        XCTAssertEqual(moved.moveCount, first.moveCount, "no slide on a move that changes nothing")
        XCTAssertEqual(moved.pinned, first.pinned)
    }

    func testEveryCornerTwiceChangesNothing() {
        for count in [4, 8] {
            for direction in [LayoutMode.topLeft, .topRight, .bottomLeft, .bottomRight] {
                var moved = state(diagonal(count, on: laptop), visible: laptop.visible)
                XCTAssertTrue(moved.moveAll(direction, visible: laptop.visible, keepOut: laptop.reserved))
                let first = moved
                XCTAssertTrue(moved.moveAll(direction, visible: laptop.visible, keepOut: laptop.reserved))
                XCTAssertEqual(moved, first, "\(count) \(direction)")
            }
        }
    }

    // MARK: thing 2, criterion 3: reading order kept, block against the named corner

    private func assertReadingOrderDiagram(_ count: Int, _ direction: LayoutMode, _ expected: [(CGFloat, CGFloat)],
                                           line: UInt = #line) {
        guard let result = ScatterLayout.reflow(old: diagonal(count, on: laptop), direction: direction,
                                                visible: laptop.visible, keepOut: laptop.reserved)
        else { return XCTFail("refused", line: line) }
        XCTAssertEqual(result.stage, 1, line: line)
        XCTAssertEqual(result.frames.map(\.origin), points(expected), line: line)
    }

    /// Right half (the quarter holds 3). Row 1: x 1104 hits the zone -> card 1 at 720. Row 2: slots
    /// 1104, 764 hold cards 2-3, card 3 rightmost. Row 3: card 4 at 1104.
    func testReadingOrderTopRightFour() {
        assertReadingOrderDiagram(4, .topRight, [(720, 41), (764, 221), (1104, 221), (1104, 401)])
    }

    /// Whole area (the half holds 7): rows of 3 / 4 / 1, each read left to right.
    func testReadingOrderTopRightEight() {
        assertReadingOrderDiagram(8, .topRight, [(40, 41), (380, 41), (720, 41), (84, 221), (424, 221), (764, 221),
                                                 (1104, 221), (1104, 401)])
    }

    /// Quarter x 16-720, y 462.5-884. The bottom row takes the LAST cards (3, 4: x 696 would end at
    /// 1016 > 720); the row above takes cards 1, 2.
    func testReadingOrderBottomLeftFour() {
        assertReadingOrderDiagram(4, .bottomLeft, [(16, 544), (356, 544), (16, 724), (356, 724)])
    }

    /// Left half (the quarter holds 4): 4 rows of 2 from the bottom, cards 7-8 at the bottom.
    func testReadingOrderBottomLeftEight() {
        assertReadingOrderDiagram(8, .bottomLeft, [(16, 184), (356, 184), (16, 364), (356, 364), (16, 544), (356, 544),
                                                   (16, 724), (356, 724)])
    }

    /// Every direction, 2-12 cards: the moved frames read (top to bottom, left to right) in the old
    /// reading order, whichever stage placed them.
    func testReadingOrderPropertyEveryDirection() {
        forEachStart { old, screen, keepOut, label in
            guard old.count >= 2 else { return }
            for direction in moves {
                guard let result = ScatterLayout.reflow(old: old, direction: direction, visible: screen.visible,
                                                        keepOut: keepOut), result.stage > 0 else { continue }
                XCTAssertEqual(readingOrder(result.frames), readingOrder(old), "\(direction) \(label)")
            }
        }
    }

    // MARK: D5 one duration per group move

    func testGroupMoveDurationUsesLargestDistance() {
        let old = [CGRect(x: 0, y: 0, width: 100, height: 100), CGRect(x: 500, y: 0, width: 100, height: 100)]
        let new = [CGRect(x: 1080, y: 0, width: 100, height: 100), CGRect(x: 600, y: 0, width: 100, height: 100)]
        XCTAssertEqual(ScatterLayout.groupMoveDuration(old: old, new: new, across: 1440),
                       ScatterLayout.moveDuration(distance: 1080, across: 1440), accuracy: 0.0001)
        XCTAssertEqual(ScatterLayout.groupMoveDuration(old: old, new: new, across: 1440), 0.3375, accuracy: 0.0001)
        XCTAssertEqual(ScatterLayout.groupMoveDuration(old: [], new: [], across: 1440), ScatterLayout.motionShort)
    }

    // MARK: D5 clamp in the short-shift return

    /// A group 10 pt past the inset area's bottom (the Dock grew) moved toward its own corner: the
    /// shift is under `visibleShift`, but the cards still come back inside the area.
    func testShortShiftReturnClampsCardOffTheArea() {
        let old = [CGRect(x: 16, y: 574, width: 320, height: 160), CGRect(x: 356, y: 734, width: 320, height: 160)]
        XCTAssertGreaterThan(old[1].maxY, laptop.area.maxY)
        guard let result = ScatterLayout.reflow(old: old, direction: .bottomLeft, visible: laptop.visible,
                                                keepOut: laptop.reserved)
        else { return XCTFail("refused") }
        XCTAssertEqual(result.stage, 0)
        XCTAssertEqual(result.frames, [old[0], old[1].offsetBy(dx: 0, dy: -10)])
        for frame in result.frames { XCTAssertTrue(laptop.area.insetBy(dx: -0.001, dy: -0.001).contains(frame)) }
        var moved = state(old, visible: laptop.visible)
        let before = moved
        XCTAssertTrue(moved.moveAll(.bottomLeft, visible: laptop.visible, keepOut: laptop.reserved))
        XCTAssertEqual(moved.moveCount, before.moveCount + 1, "a card that moved slides")
    }

    // MARK: D3 named notification zone, new cards off every keep-out, the overlap cap everywhere

    func testNotificationZoneUsesTheNamedSize() {
        let visible = CGRect(x: 0, y: 25, width: 1440, height: 875)
        XCTAssertEqual(ScatterLayout.notificationZone(visible: visible).size, ScatterLayout.notificationZoneSize)
        XCTAssertEqual(ScatterLayout.notificationZone(visible: visible).maxX, visible.maxX)
        XCTAssertEqual(ScatterLayout.notificationZone(visible: visible).minY, visible.minY)
    }

    /// Every pair overlaps by at most `maxOverlap` of the smaller card, and no card is covered past
    /// `maxCoverage`.
    private func assertCaps(_ state: ScatterLayout.LayoutState, _ label: String, line: UInt = #line) {
        let frames = state.order.compactMap { state.frames[$0] }
        for (index, frame) in frames.enumerated() {
            for other in frames[(index + 1)...] {
                let ratio = ScatterLayout.overlapArea(frame, other) / min(ScatterLayout.area(frame), ScatterLayout.area(other))
                XCTAssertLessThanOrEqual(ratio, ScatterLayout.maxOverlap + 0.0001, label, line: line)
            }
        }
        for (frame, covered) in zip(frames, ScatterLayout.coverage(frames)) {
            XCTAssertLessThanOrEqual(covered / ScatterLayout.area(frame), ScatterLayout.maxCoverage + 0.0001, label, line: line)
        }
    }

    /// Hub answer 1: the scattered, partly layered look stays. 1-12 cards added one by one (fixed
    /// keys, uniform and mixed sizes, both screens, also on top of a moved and pinned group): no card
    /// on the menu bar, the orb, the notification zone, the permission card or a highlight, and
    /// every overlap within the caps, layered keys included.
    func testNewCardsAvoidEveryKeepOutAndKeepTheCaps() {
        var layeredKeys = 0
        for screen in [laptop, ultrawide] {
            let shape = ScatterLayout.shapeKeepOut(CGRect(x: 300, y: 300, width: 400, height: 60), screenWidth: screen.screen.width)
            let keepOut = screen.reserved + [screen.permission, shape]
            for sizes in [[card], mixed] {
                for start in ["empty", "moved"] {
                    var state = ScatterLayout.LayoutState()
                    if start == "moved" {
                        state = self.state(diagonal(4, on: screen), visible: screen.visible)
                        XCTAssertTrue(state.moveAll(.topRight, visible: screen.visible, keepOut: keepOut))
                    }
                    for index in 1...12 {
                        let key = "card-\(index)"
                        if ScatterLayout.prefersLayer(key: key, existingCount: state.order.count) { layeredKeys += 1 }
                        state.cardAdded(key, size: sizes[index % sizes.count], visible: screen.visible, keepOut: keepOut)
                        let label = "\(screen.name) \(start) \(sizes.count) sizes, card \(index)"
                        for key in state.order {
                            let frame = state.frames[key]!
                            XCTAssertTrue(screen.area.insetBy(dx: -0.001, dy: -0.001).contains(frame), label)
                            for zone in keepOut {
                                XCTAssertEqual(ScatterLayout.overlapArea(frame, zone), 0, "\(label) \(key)")
                            }
                        }
                        assertCaps(state, label)
                    }
                }
            }
        }
        XCTAssertGreaterThan(layeredKeys, 0, "some keys prefer a layered spot")
    }

    /// The path behind a cap break: a pinned card (every card is pinned after a voice move) grown
    /// in place used to keep its origin with no check at all. Now it is placed again instead.
    func testPinnedCardResizeKeepsOverlapCap() {
        var state = self.state([CGRect(x: 16, y: 41, width: 320, height: 160), CGRect(x: 356, y: 41, width: 320, height: 160)],
                               visible: laptop.visible)
        XCTAssertTrue(state.pinned.contains("k0"))
        // 500 wide from x 16 covers x 356-516 of k1: 50 % of the smaller card.
        state.cardResized("k0", size: CGSize(width: 500, height: 160), visible: laptop.visible, keepOut: laptop.reserved)
        XCTAssertEqual(state.frames["k0"]?.size, CGSize(width: 500, height: 160))
        XCTAssertEqual(state.frames["k1"], CGRect(x: 356, y: 41, width: 320, height: 160), "the other card stays")
        assertCaps(state, "pinned resize")
        // A small growth that stays under the cap keeps the pinned card where it is.
        var small = self.state([CGRect(x: 16, y: 41, width: 320, height: 160), CGRect(x: 356, y: 41, width: 320, height: 160)],
                               visible: laptop.visible)
        small.cardResized("k0", size: CGSize(width: 320, height: 200), visible: laptop.visible, keepOut: laptop.reserved)
        XCTAssertEqual(small.frames["k0"], CGRect(x: 16, y: 41, width: 320, height: 200))
    }

    /// An unpinned card growing over a pinned one (a new answer next to a moved group) is held to
    /// the cap as well; it used to ignore pinned cards.
    func testUnpinnedCardResizeKeepsOverlapCapOverPinnedCards() {
        var state = self.state([CGRect(x: 356, y: 41, width: 320, height: 160)], visible: laptop.visible)
        state.cardAdded("new", size: CGSize(width: 320, height: 160), visible: laptop.visible, keepOut: laptop.reserved)
        XCTAssertNotNil(state.frames["new"])
        state.cardResized("new", size: CGSize(width: 1000, height: 600), visible: laptop.visible, keepOut: laptop.reserved)
        assertCaps(state, "unpinned over pinned")
    }

    // MARK: thing 3a, criterion 5: the idle clear is a restorable hide

    private func shownState() -> (cards: [HUDCard], layout: ScatterLayout.LayoutState) {
        var layout = state(diagonal(3, on: laptop), visible: laptop.visible)
        layout.moveAll(.left, visible: laptop.visible, keepOut: laptop.reserved)
        layout.cardDragged("k1", to: CGRect(x: 600, y: 500, width: 320, height: 160), visible: laptop.visible)
        let cards = (0..<3).map { HUDCard.info(InfoCard(id: "k\($0)", title: "Card \($0)", body: ["Body"])) }
        return (cards, layout)
    }

    func testIdleStashIsRestorableForTenMinutes() {
        let shown = shownState()
        let clearedAt = Date(timeIntervalSince1970: 1_000_000)
        let stash = ScatterLayout.IdleStash(cards: shown.cards, layout: shown.layout, clearedAt: clearedAt)
        XCTAssertEqual(ScatterLayout.IdleStash.retention, 600)
        XCTAssertTrue(stash.restorable(now: clearedAt))
        XCTAssertTrue(stash.restorable(now: clearedAt.addingTimeInterval(599)))
        XCTAssertFalse(stash.restorable(now: clearedAt.addingTimeInterval(600)))
    }

    /// The model's flow: stash, drop every card (`keep([])`, as `cards = []` does), then restore:
    /// the same frames, order and pins.
    func testIdleStashRestoresTheSameFramesOrderAndPins() {
        let shown = shownState()
        var live = shown.layout
        let stash = ScatterLayout.IdleStash(cards: shown.cards, layout: live, clearedAt: Date())
        live.keep([])
        XCTAssertTrue(live.frames.isEmpty)
        let restored = stash.layout
        XCTAssertEqual(stash.cards, shown.cards)
        XCTAssertEqual(restored.frames, shown.layout.frames)
        XCTAssertEqual(restored.order, shown.layout.order)
        XCTAssertEqual(restored.pinned, shown.layout.pinned)
        XCTAssertEqual(restored.visibleKeys, ["k0", "k2", "k1"])
    }

    /// One stash only: a later idle clear replaces the earlier one.
    func testSecondIdleStashReplacesTheFirst() {
        let shown = shownState()
        var slot: ScatterLayout.IdleStash? = ScatterLayout.IdleStash(cards: shown.cards, layout: shown.layout,
                                                                     clearedAt: Date(timeIntervalSince1970: 0))
        slot = ScatterLayout.IdleStash(cards: [shown.cards[0]], layout: ScatterLayout.LayoutState(),
                                       clearedAt: Date(timeIntervalSince1970: 100))
        XCTAssertEqual(slot?.cards, [shown.cards[0]])
        XCTAssertEqual(slot?.clearedAt, Date(timeIntervalSince1970: 100))
    }

    func testClearDropsIdleStash() {
        XCTAssertFalse(ScatterLayout.IdleStash.survives(.clear, cardsOnScreen: false))
        XCTAssertFalse(ScatterLayout.IdleStash.survives(.clear, cardsOnScreen: true))
    }

    func testNewCardDropsIdleStash() {
        XCTAssertFalse(ScatterLayout.IdleStash.survives(.newCard, cardsOnScreen: false))
        XCTAssertFalse(ScatterLayout.IdleStash.survives(.newShape, cardsOnScreen: false))
    }

    func testModeChangeDropsIdleStashOnlyWithCardsShown() {
        for mode in moves + [.aside, .restore] {
            XCTAssertFalse(ScatterLayout.IdleStash.survives(.layout(mode), cardsOnScreen: true), "\(mode)")
            XCTAssertTrue(ScatterLayout.IdleStash.survives(.layout(mode), cardsOnScreen: false), "\(mode)")
        }
        for mode in [LayoutMode.hide, .show] {
            XCTAssertTrue(ScatterLayout.IdleStash.survives(.layout(mode), cardsOnScreen: true), "\(mode)")
        }
    }
}
