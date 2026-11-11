import XCTest
@testable import OverlayCore

/// f52-movefix: voice moves keep every card readable. Top-left points, y down.
/// Laptop: 1440x900 screen, 25 pt menu bar, no Dock -> inset area x 16-1424, y 41-884,
/// notification zone x 1060-1440 y 25-155, orb keep-out x 1348-1464 y 768-924.
/// Ultrawide: 3440x1440 screen -> inset area x 16-3424, y 41-1424, zone x 3060-3440 y 25-155.
private struct Screen {
    let name: String
    let screen: CGRect
    let visible: CGRect
    var area: CGRect { ScatterLayout.insetArea(visible) }
    var keepOut: [CGRect] { [ScatterLayout.orbKeepOut(screen: screen), ScatterLayout.notificationZone(visible: visible)] }
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

final class MoveFixTests: XCTestCase {

    // MARK: helpers

    /// n cards on a diagonal across the whole width (too wide for any shift): card k is k-th in
    /// reading order (y grows by 40 per card).
    private func diagonal(_ count: Int, on screen: Screen) -> [CGRect] {
        let span = screen.area.width - card.width
        return (0..<count).map { CGRect(origin: CGPoint(x: screen.area.minX + CGFloat($0) * span / CGFloat(count - 1),
                                                        y: 100 + CGFloat($0) * 40), size: card) }
    }

    /// A loose spread of n cards over the upper and middle screen (some overlap on a laptop).
    private func spread(_ count: Int, sizes: [CGSize], on screen: Screen) -> [CGRect] {
        let area = screen.area
        return (0..<count).map { index in
            CGRect(origin: CGPoint(x: area.minX + 84 + CGFloat(index % 4) * (area.width - 400) / 3 * 0.9,
                                   y: area.minY + 30 + CGFloat(index / 4) * (area.height - 400) / 2 * 0.8),
                   size: sizes[index % sizes.count])
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

    private func origins(_ frames: [CGRect]) -> [CGPoint] { frames.map(\.origin) }

    private func points(_ pairs: [(CGFloat, CGFloat)]) -> [CGPoint] { pairs.map { CGPoint(x: $0.0, y: $0.1) } }

    private func assertDiagram(_ count: Int, _ screen: Screen, _ expected: [(CGFloat, CGFloat)],
                               direction: LayoutMode = .topRight, line: UInt = #line) {
        guard let result = ScatterLayout.reflow(old: diagonal(count, on: screen), direction: direction,
                                                visible: screen.visible, keepOut: screen.keepOut)
        else { return XCTFail("refused", line: line) }
        XCTAssertEqual(result.stage, 1, line: line)
        XCTAssertEqual(origins(result.frames), points(expected), line: line)
    }

    private func overlaps(_ frames: [CGRect]) -> [Bool] {
        frames.indices.flatMap { i in frames.indices.filter { $0 > i }.map { ScatterLayout.overlapArea(frames[i], frames[$0]) > 0 } }
    }

    /// Every property of a voice move, for 2, 4, 8 and 12 cards, both screens, uniform and
    /// mixed sizes, with and without the permission card.
    private func assertProperties(_ direction: LayoutMode, line: UInt = #line) {
        for screen in [laptop, ultrawide] {
            let area = screen.area
            for keepOut in [screen.keepOut, screen.keepOut + [screen.permission]] {
                for sizes in [[card], mixed] {
                    for count in [2, 4, 8, 12] {
                        let label = "\(screen.name) \(direction) \(count) \(sizes.count) \(keepOut.count)"
                        let old = spread(count, sizes: sizes, on: screen)
                        let before = state(old, visible: screen.visible)
                        var moved = before
                        guard let result = ScatterLayout.reflow(old: old, direction: direction, visible: screen.visible,
                                                                keepOut: keepOut),
                              moved.moveAll(direction, visible: screen.visible, keepOut: keepOut)
                        else { XCTFail("refused \(label)", line: line); continue }
                        let keys = (0..<count).map { "k\($0)" }
                        let frames = keys.map { moved.frames[$0]! }
                        XCTAssertEqual(frames, result.frames, label, line: line)
                        XCTAssertEqual(frames.map(\.size), old.map(\.size), label, line: line)
                        XCTAssertTrue(moved.pinned.isSuperset(of: keys), label, line: line)
                        for frame in frames {
                            XCTAssertTrue(area.insetBy(dx: -0.001, dy: -0.001).contains(frame), label, line: line)
                            for zone in keepOut { XCTAssertEqual(ScatterLayout.overlapArea(frame, zone), 0, label, line: line) }
                        }
                        switch result.stage {
                        case 0:
                            XCTAssertEqual(overlaps(frames), overlaps(old), "a shift adds no overlap: \(label)", line: line)
                        case 1:
                            XCTAssertFalse(overlaps(frames).contains(true), "rows never overlap: \(label)", line: line)
                            assertRowsKeepReadingOrder(old: old, new: frames, direction: direction, area: area,
                                                       visible: screen.visible, label: label, line: line)
                            assertTouchesNamedEdges(frames, direction: direction, old: old, area: area,
                                                    visible: screen.visible, keepOut: keepOut, label: label, line: line)
                        default:
                            assertCornersUncovered(moved, label: label, line: line)
                        }
                    }
                }
            }
        }
    }

    /// The anchor of a move: named sides, an unnamed axis from the old group's centre; otherSide
    /// toward the half away from the group's centre.
    private func anchor(_ direction: LayoutMode, old: [CGRect], area: CGRect, visible: CGRect) -> (right: Bool?, bottom: Bool?, resolvedRight: Bool, resolvedBottom: Bool) {
        let group = old.reduce(CGRect.null) { $0.union($1) }
        let named: (Bool?, Bool?)
        switch direction {
        case .left: named = (false, nil)
        case .right: named = (true, nil)
        case .up: named = (nil, false)
        case .down: named = (nil, true)
        case .topLeft: named = (false, false)
        case .topRight: named = (true, false)
        case .bottomLeft: named = (false, true)
        case .bottomRight: named = (true, true)
        default: named = (group.midX <= visible.midX, nil)
        }
        return (named.0, named.1, named.0 ?? (group.midX > area.midX), named.1 ?? (group.midY > area.midY))
    }

    /// f52-cardfix: the new frames read (top to bottom, then left to right) in the old reading order.
    private func assertRowsKeepReadingOrder(old: [CGRect], new: [CGRect], direction: LayoutMode, area: CGRect,
                                            visible: CGRect, label: String, line: UInt) {
        let reading = old.indices.sorted { (old[$0].minY, old[$0].minX, $0) < (old[$1].minY, old[$1].minX, $1) }
        let readNew = new.indices.sorted { (new[$0].minY, new[$0].minX, $0) < (new[$1].minY, new[$1].minX, $1) }
        XCTAssertEqual(readNew, reading, "reading order: \(label)", line: line)
    }

    /// The group touches each named edge within 0.5 pt, or stops `minGap` short of a keep-out
    /// that took the anchor slot.
    private func assertTouchesNamedEdges(_ frames: [CGRect], direction: LayoutMode, old: [CGRect], area: CGRect,
                                         visible: CGRect, keepOut: [CGRect], label: String, line: UInt) {
        let side = anchor(direction, old: old, area: area, visible: visible)
        let box = frames.reduce(CGRect.null) { $0.union($1) }
        let gap = ScatterLayout.minGap
        if let right = side.right {
            var touches: Bool
            if right {
                touches = abs(box.maxX - area.maxX) <= 0.5
                if !touches { touches = keepOut.contains(where: { abs($0.minX - gap - box.maxX) <= 0.5 }) }
            } else {
                touches = abs(box.minX - area.minX) <= 0.5
                if !touches { touches = keepOut.contains(where: { abs($0.maxX + gap - box.minX) <= 0.5 }) }
            }
            XCTAssertTrue(touches, "x edge: \(label)", line: line)
        }
        if let bottom = side.bottom {
            XCTAssertEqual(bottom ? box.maxY : box.minY, bottom ? area.maxY : area.minY, accuracy: 0.5, "y edge: \(label)", line: line)
        }
    }

    /// Stage 2: every card's top-left `minStrip` square is uncovered by every card in front of it.
    private func assertCornersUncovered(_ state: ScatterLayout.LayoutState, label: String, line: UInt) {
        let stack = state.order.map { state.frames[$0]! }
        for (index, frame) in stack.enumerated() {
            let corner = CGRect(origin: frame.origin, size: CGSize(width: ScatterLayout.minStrip, height: ScatterLayout.minStrip))
            for front in stack[(index + 1)...] {
                XCTAssertEqual(ScatterLayout.overlapArea(corner, front), 0, "corner covered: \(label)", line: line)
            }
        }
    }

    // MARK: reserved zone

    func testNotificationZoneIsTopRightOfTheVisibleFrame() {
        XCTAssertEqual(ScatterLayout.notificationZone(visible: laptop.visible), CGRect(x: 1060, y: 25, width: 380, height: 130))
        XCTAssertEqual(ScatterLayout.notificationZone(visible: ultrawide.visible), CGRect(x: 3060, y: 25, width: 380, height: 130))
    }

    // MARK: D1 diagrams, top_right (exact origins; cards 320x160, gap 20)
    // f52-cardfix rule: a top anchor fills the top row with the first cards; a right anchor walks
    // each row's slots from the right edge and gives the rightmost slot the row's LAST card, so
    // every row reads left to right in the old reading order (card 1 first).

    /// Quarter x 720-1424, y 41-462.5. Row 1 (y 41): x 1104 hits the zone, the row jumps to end
    /// at 1040 -> x 720; x 380 is past the quarter. Row 2 (y 221): x 1104.
    func testDiagramLaptopTwoCards() {
        assertDiagram(2, laptop, [(720, 41), (1104, 221)])
    }

    /// The quarter holds 3 (row 3 would end at y 561 > 462.5): the right half (x 720-1424, holds 7).
    /// Row 1 (y 41): card 1 at 720 (zone jump). Row 2 (y 221): slots 1104, 764 take cards 2-3, card 3
    /// rightmost -> card 2 at 764, card 3 at 1104. Row 3 (y 401): card 4 at 1104.
    func testDiagramLaptopFourCards() {
        assertDiagram(4, laptop, [(720, 41), (764, 221), (1104, 221), (1104, 401)])
    }

    /// Right half holds 7 (row 5 would end at y 921 > 884): the whole area, rows of 3 / 4 / 1.
    /// Row 1 slots 720, 380, 40 hold cards 1-3 (card 3 at 720); row 2 slots 1104, 764, 424, 84 hold
    /// cards 4-7 (card 7 at 1104); row 3: card 8 at 1104.
    func testDiagramLaptopEightCards() {
        assertDiagram(8, laptop, [(40, 41), (380, 41), (720, 41), (84, 221), (424, 221), (764, 221), (1104, 221), (1104, 401)])
    }

    /// Rows of 3 / 4 / 4 / 1, each read left to right. Row 4 (y 581) ends at y 741, above the orb
    /// keep-out (768).
    func testDiagramLaptopTwelveCards() {
        assertDiagram(12, laptop, [(40, 41), (380, 41), (720, 41), (84, 221), (424, 221), (764, 221), (1104, 221),
                                   (84, 401), (424, 401), (764, 401), (1104, 401), (1104, 581)])
    }

    /// Quarter x 1720-3424, y 41-732.5. Row 1: x 3104 hits the zone -> slots 2720, 2380, 2040 (1700
    /// is past the quarter). Rows 2 and 3: slots 3104, 2764, 2424, 2084, 1744. The quarter holds 13.
    /// Each row's run sits left to right, its last card in the rightmost slot.
    func testDiagramUltrawideTwoCards() {
        assertDiagram(2, ultrawide, [(2380, 41), (2720, 41)])
    }

    func testDiagramUltrawideFourCards() {
        assertDiagram(4, ultrawide, [(2040, 41), (2380, 41), (2720, 41), (3104, 221)])
    }

    func testDiagramUltrawideEightCards() {
        assertDiagram(8, ultrawide, [(2040, 41), (2380, 41), (2720, 41), (1744, 221), (2084, 221), (2424, 221), (2764, 221), (3104, 221)])
    }

    func testDiagramUltrawideTwelveCards() {
        assertDiagram(12, ultrawide, [(2040, 41), (2380, 41), (2720, 41), (1744, 221), (2084, 221), (2424, 221), (2764, 221),
                                      (3104, 221), (2084, 401), (2424, 401), (2764, 401), (3104, 401)])
    }

    /// `left` from an upper group (centre above the area's centre): left half x 16-720, rows from the top.
    func testDiagramLaptopLeftFourCards() {
        assertDiagram(4, laptop, [(16, 41), (356, 41), (16, 221), (356, 221)], direction: .left)
    }

    // MARK: properties, one function per direction

    func testPropertyLeft() { assertProperties(.left) }
    func testPropertyRight() { assertProperties(.right) }
    func testPropertyUp() { assertProperties(.up) }
    func testPropertyDown() { assertProperties(.down) }
    func testPropertyTopLeft() { assertProperties(.topLeft) }
    func testPropertyTopRight() { assertProperties(.topRight) }
    func testPropertyBottomLeft() { assertProperties(.bottomLeft) }
    func testPropertyBottomRight() { assertProperties(.bottomRight) }
    func testPropertyOtherSide() { assertProperties(.otherSide) }

    /// The mixed set with the flowchart card on the laptop really reaches every stage it needs:
    /// a top-right move of 12 mixed cards cannot fit in rows and takes the grid.
    func testPropertyMixedSizesUseTheGridOnlyWhenRowsCannotFit() {
        let old = spread(12, sizes: mixed, on: laptop)
        XCTAssertEqual(ScatterLayout.reflow(old: old, direction: .topRight, visible: laptop.visible, keepOut: laptop.keepOut)?.stage, 2)
        XCTAssertEqual(ScatterLayout.reflow(old: spread(4, sizes: mixed, on: laptop), direction: .topRight,
                                            visible: laptop.visible, keepOut: laptop.keepOut)?.stage, 1)
    }

    // MARK: stage 0 (criterion 3)

    func testStageZeroKeepsAHandLayoutWithItsOverlap() {
        let old = [CGRect(x: 500, y: 400, width: 300, height: 150), CGRect(x: 700, y: 480, width: 300, height: 150)]
        let overlap = ScatterLayout.overlapArea(old[0], old[1])
        XCTAssertGreaterThan(overlap, 0)
        for direction in [LayoutMode.topLeft, .topRight, .bottomRight] {
            guard let result = ScatterLayout.reflow(old: old, direction: direction, visible: laptop.visible, keepOut: laptop.keepOut)
            else { return XCTFail("\(direction) refused") }
            XCTAssertEqual(result.stage, 0, "\(direction)")
            let dx = result.frames[0].minX - old[0].minX, dy = result.frames[0].minY - old[0].minY
            XCTAssertEqual(result.frames[1], old[1].offsetBy(dx: dx, dy: dy), "\(direction)")
            XCTAssertEqual(ScatterLayout.overlapArea(result.frames[0], result.frames[1]), overlap, accuracy: 0.001, "\(direction)")
            for frame in result.frames {
                for zone in laptop.keepOut { XCTAssertEqual(ScatterLayout.overlapArea(frame, zone), 0, "\(direction)") }
            }
        }
        // top_left touches both edges exactly.
        let topLeft = ScatterLayout.reflow(old: old, direction: .topLeft, visible: laptop.visible, keepOut: laptop.keepOut)
        XCTAssertEqual(topLeft?.frames.first?.origin, CGPoint(x: 16, y: 41))
    }

    func testGroupAlreadyInPlaceIsDoneWithoutMotion() {
        let frames = [CGRect(x: 20, y: 45, width: 300, height: 150), CGRect(x: 220, y: 125, width: 300, height: 150)]
        var moved = state(frames, visible: laptop.visible)
        let before = moved
        XCTAssertTrue(moved.moveAll(.topLeft, visible: laptop.visible, keepOut: laptop.keepOut))
        XCTAssertEqual(moved.frames, before.frames)
        XCTAssertEqual(moved.order, before.order)
    }

    // MARK: stage 2 and refusal (criterion 4)

    func testEighteenLargeCardsTakeTheGridWithCornersUncovered() {
        let old = (0..<18).map { CGRect(x: 16 + CGFloat($0 % 6) * 170, y: 41 + CGFloat($0 / 6) * 180, width: 400, height: 300) }
        for direction in [LayoutMode.left, .topRight, .bottomRight, .down] {
            var moved = state(old, visible: laptop.visible)
            XCTAssertEqual(ScatterLayout.reflow(old: old, direction: direction, visible: laptop.visible, keepOut: laptop.keepOut)?.stage, 2)
            XCTAssertTrue(moved.moveAll(direction, visible: laptop.visible, keepOut: laptop.keepOut))
            XCTAssertEqual(Set(moved.order), Set((0..<18).map { "k\($0)" }))
            for key in moved.order {
                let frame = moved.frames[key]!
                XCTAssertEqual(frame.size, CGSize(width: 400, height: 300))
                XCTAssertTrue(laptop.area.insetBy(dx: -0.001, dy: -0.001).contains(frame), "\(direction) \(key)")
                for zone in laptop.keepOut { XCTAssertEqual(ScatterLayout.overlapArea(frame, zone), 0, "\(direction) \(key)") }
            }
            assertCornersUncovered(moved, label: "\(direction)", line: #line)
        }
    }

    func testNoRoomEvenForTheGridRefusesAndChangesNothing() {
        let old = Array(repeating: CGRect(x: 16, y: 41, width: 1300, height: 800), count: 18)
        XCTAssertNil(ScatterLayout.reflow(old: old, direction: .left, visible: laptop.visible, keepOut: laptop.keepOut))
        var shown = state(old, visible: laptop.visible)
        let before = shown
        XCTAssertFalse(shown.moveAll(.bottomRight, visible: laptop.visible, keepOut: laptop.keepOut))
        XCTAssertEqual(shown, before)
        var hidden = before
        hidden.hide()
        let hiddenBefore = hidden
        XCTAssertFalse(hidden.moveAll(.left, visible: laptop.visible, keepOut: laptop.keepOut))
        XCTAssertEqual(hidden, hiddenBefore, "still hidden, same frames")
        var parked = before
        parked.aside()
        let parkedBefore = parked
        XCTAssertFalse(parked.moveAll(.topRight, visible: laptop.visible, keepOut: laptop.keepOut))
        XCTAssertEqual(parked, parkedBefore, "still parked")
    }

    // MARK: drag after a voice move (criterion 5)

    func testDragAfterVoiceMoveUpdatesStoredFrame() {
        var moved = state(diagonal(4, on: laptop), visible: laptop.visible)
        XCTAssertTrue(moved.moveAll(.topRight, visible: laptop.visible, keepOut: laptop.keepOut))
        let dropped = CGRect(x: 300, y: 500, width: 320, height: 160)
        moved.cardDragged("k1", to: dropped, visible: laptop.visible)
        XCTAssertEqual(moved.frames["k1"], dropped)
        XCTAssertTrue(moved.pinned.contains("k1"))
        XCTAssertEqual(moved.order.last, "k1")
        // Past the screen edge: clamped inside the visible frame.
        moved.cardDragged("k1", to: dropped.offsetBy(dx: 2000, dy: 0), visible: laptop.visible)
        XCTAssertEqual(moved.frames["k1"]?.maxX, laptop.visible.maxX)
        moved.cardDragged("k1", to: dropped, visible: laptop.visible)
        // The next voice move takes the dragged card with the group.
        XCTAssertTrue(moved.moveAll(.left, visible: laptop.visible, keepOut: laptop.keepOut))
        XCTAssertNotEqual(moved.frames["k1"], dropped)
        let box = moved.order.compactMap { moved.frames[$0] }.reduce(CGRect.null) { $0.union($1) }
        XCTAssertEqual(box.minX, laptop.area.minX, accuracy: 0.5)
    }
}
