import XCTest
@testable import OverlayCore

// A 1440x900 screen with a 25 pt menu bar, no Dock: the visible frame in top-left points.
private let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
private let visible = CGRect(x: 0, y: 25, width: 1440, height: 875)
private let area = ScatterLayout.insetArea(visible)
/// Placement room for cards a test then drags to exact spots (nothing is retired on the way).
private let roomy = CGRect(x: 0, y: 0, width: 20000, height: 20000)
private let moves: [LayoutMode] = [.left, .right, .up, .down, .topLeft, .topRight, .bottomLeft, .bottomRight, .otherSide]

final class HideMoveTests: XCTestCase {

    private func decode(_ line: String) -> OverlayEvent {
        OverlayEvent.decode(line: Data(line.utf8))
    }

    /// Three cards placed in a small region left of the centre line, the second one pinned.
    private func compactState() -> ScatterLayout.LayoutState {
        var state = ScatterLayout.LayoutState()
        let region = CGRect(x: 100, y: 250, width: 600, height: 400)
        for i in 1...3 {
            state.cardAdded("c\(i)", size: CGSize(width: 220, height: 90), visible: region, keepOut: [])
        }
        XCTAssertEqual(state.order.count, 3)
        if let frame = state.frames["c2"] { state.cardDragged("c2", to: frame, visible: visible) }
        return state
    }

    private func group(_ state: ScatterLayout.LayoutState) -> CGRect {
        state.order.compactMap { state.frames[$0] }.reduce(CGRect.null) { $0.union($1) }
    }

    // MARK: decode (criterion 8)

    func testDecodesEveryLayoutMode() {
        let wire: [(String, LayoutMode)] = [
            ("aside", .aside), ("restore", .restore), ("hide", .hide), ("show", .show),
            ("left", .left), ("right", .right), ("up", .up), ("down", .down),
            ("top_left", .topLeft), ("top_right", .topRight), ("bottom_left", .bottomLeft),
            ("bottom_right", .bottomRight), ("other_side", .otherSide),
        ]
        XCTAssertEqual(wire.count, 13)
        XCTAssertEqual(LayoutMode.allCases.count, 13)
        for (raw, mode) in wire {
            XCTAssertEqual(decode(#"{"type":"layout","mode":"\#(raw)"}"#), .layout(mode), raw)
        }
    }

    func testUnknownLayoutModesDecodeAsUnknown() {
        for raw in ["Hide", "move", "other_screen", "topLeft", "HIDE", ""] {
            XCTAssertEqual(decode(#"{"type":"layout","mode":"\#(raw)"}"#), .unknown, raw)
        }
    }

    // MARK: hide / show (criterion 9a, 9b)

    func testHideThenShowRestoresExactly() {
        for parked in [false, true] {
            var state = compactState()
            if parked { state.aside() }
            let before = state
            state.hide()
            XCTAssertTrue(state.hidden)
            XCTAssertTrue(state.chipKeys.isEmpty)
            XCTAssertTrue(state.visibleKeys.isEmpty)
            let hidden = state
            state.hide()
            XCTAssertEqual(state, hidden, "hide repeats as a no-op")
            state.show()
            XCTAssertFalse(state.hidden)
            XCTAssertEqual(state.frames, before.frames)
            XCTAssertEqual(state.order, before.order)
            XCTAssertEqual(state.pinned, before.pinned)
            XCTAssertEqual(state.waiting, before.waiting)
            XCTAssertEqual(state.mode, before.mode)
            XCTAssertEqual(state, before)
            state.show()
            XCTAssertEqual(state, before, "show repeats as a no-op")
            XCTAssertEqual(parked ? state.chipKeys : state.visibleKeys, before.order)
        }
    }

    func testNewCardWhileHiddenShowsEverything() {
        var state = compactState()
        state.hide()
        // OverlayModel.add(card:) un-hides first, then the panel places the measured card.
        state.show()
        state.cardAdded("c4", size: CGSize(width: 220, height: 90), visible: visible, keepOut: [])
        XCTAssertEqual(Set(state.visibleKeys), ["c1", "c2", "c3", "c4"])
    }

    // MARK: move (criterion 9c, 9d, 9e)

    func testMoveKeepsLayoutAndTouchesTheEdge() {
        for direction in moves {
            var state = compactState()
            let before = state
            let oldGroup = group(before)
            state.moveAll(direction, visible: visible)
            let newGroup = group(state)
            let dx = newGroup.minX - oldGroup.minX, dy = newGroup.minY - oldGroup.minY
            for key in before.order {
                guard let old = before.frames[key], let new = state.frames[key] else { return XCTFail("\(key) lost") }
                XCTAssertTrue(visible.contains(new), "\(direction) \(key)")
                XCTAssertEqual(new.size, old.size, "\(direction) \(key)")
                // One translation for the whole group: the relative layout is kept on both axes.
                XCTAssertEqual(new.minX - old.minX, dx, accuracy: 0.001, "\(direction) \(key)")
                XCTAssertEqual(new.minY - old.minY, dy, accuracy: 0.001, "\(direction) \(key)")
            }
            XCTAssertEqual(state.order, before.order)
            XCTAssertTrue(state.pinned.isSuperset(of: before.order), "\(direction)")
            switch direction {
            case .left: XCTAssertEqual(newGroup.minX, area.minX, accuracy: 0.001); XCTAssertEqual(dy, 0)
            case .right: XCTAssertEqual(newGroup.maxX, area.maxX, accuracy: 0.001); XCTAssertEqual(dy, 0)
            case .up: XCTAssertEqual(newGroup.minY, area.minY, accuracy: 0.001); XCTAssertEqual(dx, 0)
            case .down: XCTAssertEqual(newGroup.maxY, area.maxY, accuracy: 0.001); XCTAssertEqual(dx, 0)
            case .topLeft:
                XCTAssertEqual(newGroup.minX, area.minX, accuracy: 0.001)
                XCTAssertEqual(newGroup.minY, area.minY, accuracy: 0.001)
            case .topRight:
                XCTAssertEqual(newGroup.maxX, area.maxX, accuracy: 0.001)
                XCTAssertEqual(newGroup.minY, area.minY, accuracy: 0.001)
            case .bottomLeft:
                XCTAssertEqual(newGroup.minX, area.minX, accuracy: 0.001)
                XCTAssertEqual(newGroup.maxY, area.maxY, accuracy: 0.001)
            case .bottomRight:
                XCTAssertEqual(newGroup.maxX, area.maxX, accuracy: 0.001)
                XCTAssertEqual(newGroup.maxY, area.maxY, accuracy: 0.001)
            case .otherSide:
                XCTAssertEqual(newGroup.minX, 2 * visible.midX - oldGroup.maxX, accuracy: 0.001)
                XCTAssertEqual(dy, 0)
            default: XCTFail("not a move: \(direction)")
            }
            XCTAssertEqual(state.moveCount, before.moveCount + 1)
            assertInNamedPart(newGroup, direction)
        }
    }

    /// The group ends inside the named half (edges) or quarter (corners).
    private func assertInNamedPart(_ box: CGRect, _ direction: LayoutMode, line: UInt = #line) {
        let eps: CGFloat = 0.001
        switch direction {
        case .left, .topLeft, .bottomLeft: XCTAssertLessThanOrEqual(box.maxX, area.midX + eps, line: line)
        case .right, .topRight, .bottomRight: XCTAssertGreaterThanOrEqual(box.minX, area.midX - eps, line: line)
        default: break
        }
        switch direction {
        case .up, .topLeft, .topRight: XCTAssertLessThanOrEqual(box.maxY, area.midY + eps, line: line)
        case .down, .bottomLeft, .bottomRight: XCTAssertGreaterThanOrEqual(box.minY, area.midY - eps, line: line)
        default: break
        }
    }

    /// Twelve cards spread edge to edge (a 4x3 grid): a plain shift would not show, so every move
    /// squeezes the group into the named part, order kept.
    func testScatteredGroupStillMovesVisibly() {
        var scattered = ScatterLayout.LayoutState()
        let columns: [CGFloat] = [16, 382, 748, 1124], rows: [CGFloat] = [41, 300, 560]
        var i = 0
        for y in rows {
            for x in columns {
                i += 1
                let spot = CGRect(x: x, y: y, width: 300, height: 110)
                scattered.cardAdded("s\(i)", size: spot.size, visible: roomy, keepOut: [])
                scattered.cardDragged("s\(i)", to: spot, visible: visible)
            }
        }
        XCTAssertEqual(scattered.order.count, 12)
        XCTAssertEqual(group(scattered).width, area.width, accuracy: 0.001)
        for direction in moves where direction != .otherSide {
            var state = scattered
            state.moveAll(direction, visible: visible)
            let box = group(state)
            assertInNamedPart(box, direction)
            let horizontal: Set<LayoutMode> = [.left, .right, .topLeft, .topRight, .bottomLeft, .bottomRight]
            let vertical: Set<LayoutMode> = [.up, .down, .topLeft, .topRight, .bottomLeft, .bottomRight]
            let byX = scattered.order.sorted { scattered.frames[$0]!.minX < scattered.frames[$1]!.minX }
            let byY = scattered.order.sorted { scattered.frames[$0]!.minY < scattered.frames[$1]!.minY }
            for (a, b) in zip(byX, byX.dropFirst()) {
                XCTAssertLessThanOrEqual(state.frames[a]!.minX, state.frames[b]!.minX + 0.001, "\(direction)")
            }
            for (a, b) in zip(byY, byY.dropFirst()) {
                XCTAssertLessThanOrEqual(state.frames[a]!.minY, state.frames[b]!.minY + 0.001, "\(direction)")
            }
            for key in scattered.order {
                let old = scattered.frames[key]!, new = state.frames[key]!
                XCTAssertEqual(new.size, old.size)
                XCTAssertTrue(visible.contains(new), "\(direction) \(key)")
                if !horizontal.contains(direction) { XCTAssertEqual(new.minX, old.minX, accuracy: 0.001) }
                if !vertical.contains(direction) { XCTAssertEqual(new.minY, old.minY, accuracy: 0.001) }
            }
            if horizontal.contains(direction) {
                let edge = [.left, .topLeft, .bottomLeft].contains(direction) ? box.minX - area.minX : area.maxX - box.maxX
                XCTAssertEqual(edge, 0, accuracy: 0.001, "\(direction)")
            }
        }
    }

    func testOtherSideOfAStraddlingGroupGoesToTheFarHalf() {
        var state = ScatterLayout.LayoutState()
        // Group 600...1070 straddles the midline (720); its centre (835) is right of it.
        let spots = [CGRect(x: 600, y: 300, width: 220, height: 90), CGRect(x: 850, y: 300, width: 220, height: 90),
                     CGRect(x: 700, y: 450, width: 220, height: 90)]
        for (i, spot) in spots.enumerated() {
            state.cardAdded("c\(i)", size: spot.size, visible: visible, keepOut: [])
            state.cardDragged("c\(i)", to: spot, visible: visible)
        }
        XCTAssertLessThan(group(state).minX, visible.midX)
        XCTAssertGreaterThan(group(state).maxX, visible.midX)
        state.moveAll(.otherSide, visible: visible)
        XCTAssertLessThanOrEqual(group(state).maxX, area.midX + 0.001)
        XCTAssertEqual(group(state).minX, area.minX, accuracy: 0.001)
    }

    func testPackedSqueezesInOrderAndTouchesTheEdge() {
        let starts: [CGFloat] = [0, 300, 900], lengths: [CGFloat] = [200, 200, 300]
        let row = [CGFloat(0)...100, 0...100, 0...100], limit = CGFloat(0)...1000
        let left = ScatterLayout.packed(starts, lengths: lengths, across: row, from: 10, to: 610, atEnd: false, limit: limit)
        XCTAssertEqual(left[0], 10, accuracy: 0.001)
        XCTAssertEqual(left[2] + 300, 610, accuracy: 0.001)
        XCTAssertLessThan(left[0], left[1]); XCTAssertLessThan(left[1], left[2])
        let right = ScatterLayout.packed(starts, lengths: lengths, across: row, from: 10, to: 610, atEnd: true, limit: limit)
        XCTAssertEqual(right[2] + 300, 610, accuracy: 0.001)
        XCTAssertGreaterThanOrEqual(right[0], 10 - 0.001)
        // A group that fits keeps its spacing: one shared offset.
        let fits = ScatterLayout.packed([100, 250], lengths: [100, 100], across: [0...100, 0...100],
                                        from: 0, to: 600, atEnd: true, limit: 0...600)
        XCTAssertEqual(fits, [350, 500])
        // Longer than the half but a shift of 24 pt or more shows: plain shift, spacing kept.
        let shifted = ScatterLayout.packed([400, 1000], lengths: [300, 300], across: [0...100, 0...100],
                                           from: 16, to: 720, atEnd: false, limit: 16...1424)
        XCTAssertEqual(shifted, [16, 616])
    }

    /// Squeezed cards keep a 44 pt strip each, never scale 0, and run past the half when needed.
    func testSqueezeKeepsAMinimumStripPerCard() {
        let starts: [CGFloat] = [0, 10, 20, 900]
        let row = Array(repeating: CGFloat(0)...100, count: 4)
        let out = ScatterLayout.packed(starts, lengths: [100, 100, 100, 100], across: row,
                                       from: 0, to: 200, atEnd: false, limit: 0...1000)
        XCTAssertEqual(out, [0, 44, 88, 132])
        let fromEnd = ScatterLayout.packed(starts, lengths: [100, 100, 100, 100], across: row,
                                           from: 800, to: 1000, atEnd: true, limit: 0...1000)
        XCTAssertEqual(fromEnd[3], 900, accuracy: 0.001)
        for (a, b) in zip(fromEnd.sorted(), fromEnd.sorted().dropFirst()) { XCTAssertGreaterThanOrEqual(b - a, 44 - 0.001) }
        // Cards in different rows cannot cover each other: no strip is forced between them.
        let rows = [CGFloat(0)...100, 200...300]
        XCTAssertEqual(ScatterLayout.packed([0, 0, ], lengths: [100, 100], across: rows, from: 0, to: 50,
                                            atEnd: false, limit: 0...40), [0, 0])
    }

    /// A tall column on a 1440x900 screen moved up and down: each card keeps its strip.
    func testTallColumnKeepsItsStrips() {
        for (count, height, step) in [(5, CGFloat(250), CGFloat(148)), (13, 100, 60)] {
            var state = ScatterLayout.LayoutState()
            for i in 0..<count {
                state.cardAdded("k\(i)", size: CGSize(width: 220, height: height), visible: roomy, keepOut: [])
                state.cardDragged("k\(i)", to: CGRect(x: 100, y: 41 + CGFloat(i) * step, width: 220, height: height),
                                  visible: visible)
            }
            for direction in [LayoutMode.up, .down] {
                var moved = state
                XCTAssertTrue(moved.moveAll(direction, visible: visible))
                let ys = (0..<count).map { moved.frames["k\($0)"]!.minY }
                for (a, b) in zip(ys, ys.dropFirst()) {
                    XCTAssertGreaterThanOrEqual(b - a, ScatterLayout.minStrip - 0.001, "\(count) \(direction)")
                }
                for key in moved.order { XCTAssertTrue(visible.contains(moved.frames[key]!), "\(count) \(direction)") }
            }
        }
    }

    func testMoveToTheOtherSideMirrorsAGroupOnTheLeft() {
        var state = compactState()
        state.moveAll(.left, visible: visible)
        let leftGroup = group(state)
        state.moveAll(.otherSide, visible: visible)
        XCTAssertEqual(group(state).maxX, area.maxX, accuracy: 0.001)
        XCTAssertEqual(group(state).minY, leftGroup.minY, accuracy: 0.001)
    }

    /// A tall pinned column moved right would land on the orb: the whole column shifts (one
    /// shared offset, layout kept) so the orb stays clear, and nothing leaves the screen.
    func testTallColumnNeverCoversTheOrb() {
        let orb = ScatterLayout.orbKeepOut(screen: screen)
        var state = ScatterLayout.LayoutState()
        for i in 0..<5 {
            state.cardAdded("t\(i)", size: CGSize(width: 220, height: 150), visible: visible, keepOut: [])
        }
        for i in 0..<5 {
            state.cardDragged("t\(i)", to: CGRect(x: 100, y: 41 + CGFloat(i) * 160, width: 220, height: 150), visible: visible)
        }
        let before = state
        for direction in [LayoutMode.right, .bottomRight, .down] {
            var moved = before
            moved.moveAll(direction, visible: visible, keepOut: [orb])
            let dx = moved.frames["t0"]!.minX - before.frames["t0"]!.minX
            let dy = moved.frames["t0"]!.minY - before.frames["t0"]!.minY
            for i in 0..<5 {
                let new = moved.frames["t\(i)"]!, old = before.frames["t\(i)"]!
                XCTAssertEqual(ScatterLayout.overlapArea(new, orb), 0, "\(direction) t\(i)")
                XCTAssertTrue(visible.contains(new), "\(direction) t\(i)")
                if direction == .right {
                    XCTAssertEqual(new.minX - old.minX, dx, accuracy: 0.001)
                    XCTAssertEqual(new.minY - old.minY, dy, accuracy: 0.001)
                }
            }
        }
    }

    /// No shift clears the orb: the cards stay where they were (the move is refused).
    func testMoveThatCannotClearTheOrbLeavesTheCards() {
        let orb = ScatterLayout.orbKeepOut(screen: screen)
        XCTAssertNil(ScatterLayout.clearOf([orb], [area], area: area))
        var state = ScatterLayout.LayoutState()
        state.cardAdded("big", size: area.size, visible: visible, keepOut: [])
        state.cardDragged("big", to: area, visible: visible)
        state.hide()
        let before = state
        XCTAssertFalse(state.moveAll(.right, visible: visible, keepOut: [orb]))
        XCTAssertEqual(state, before, "a refused move changes nothing: still hidden, same frames")
        state.show()
        state.aside()
        let parked = state
        XCTAssertFalse(state.moveAll(.bottomRight, visible: visible, keepOut: [orb]))
        XCTAssertEqual(state, parked, "still parked")
    }

    /// The permission card (top right) is kept clear like the orb.
    func testMoveKeepsThePermissionCardClear() {
        let permission = CGRect(x: visible.maxX - 16 - 320, y: visible.minY + 16, width: 320, height: 120)
        var state = compactState()
        XCTAssertTrue(state.moveAll(.topRight, visible: visible, keepOut: [permission]))
        for key in state.order {
            XCTAssertEqual(ScatterLayout.overlapArea(state.frames[key]!, permission), 0, key)
            XCTAssertTrue(visible.contains(state.frames[key]!))
        }
    }

    func testRefusedMoveNudgeLeansTowardTheAskedSide() {
        XCTAssertEqual(ScatterLayout.nudgeOffset(.left).dx, -ScatterLayout.nudgeDistance)
        XCTAssertEqual(ScatterLayout.nudgeOffset(.bottomRight).dy, ScatterLayout.nudgeDistance)
        XCTAssertEqual(ScatterLayout.nudgeOffset(.hide), CGVector(dx: 0, dy: 0))
        XCTAssertLessThan(ScatterLayout.nudgeDuration, ScatterLayout.motionShort)
    }

    func testMoveDurationScalesWithDistance() {
        XCTAssertEqual(ScatterLayout.moveDuration(distance: 0, across: 1440), ScatterLayout.motionShort)
        XCTAssertEqual(ScatterLayout.moveDuration(distance: 1080, across: 1440), 0.3375, accuracy: 0.0001)
        XCTAssertEqual(ScatterLayout.moveDuration(distance: 1440, across: 1440), ScatterLayout.moveMaxDuration)
        // Speed follows the screen width: a full-width move on an ultrawide takes the same time.
        XCTAssertEqual(ScatterLayout.moveDuration(distance: 5120, across: 5120), ScatterLayout.moveMaxDuration)
        XCTAssertEqual(ScatterLayout.moveDuration(distance: 3840, across: 5120), 0.3375, accuracy: 0.0001)
        XCTAssertEqual(ScatterLayout.moveDuration(distance: 100, across: 0), ScatterLayout.moveMaxDuration)
    }

    func testConfirmCardRestoresParkedCards() {
        XCTAssertTrue(ScatterLayout.restoresParked(.info(InfoCard(id: "agentconfirm", title: "Confirm", body: ["Send mail"]))))
        XCTAssertFalse(ScatterLayout.restoresParked(.info(InfoCard(id: "c1", title: "Note", body: []))))
        XCTAssertFalse(ScatterLayout.restoresParked(.info(InfoCard(title: "No id", body: []))))
    }

    func testMoveClearsTheOrb() {
        let orb = ScatterLayout.orbKeepOut(screen: screen)
        for direction in [LayoutMode.bottomRight, .down, .right] {
            var state = compactState()
            if direction == .right { state.moveAll(.down, visible: visible) }
            state.moveAll(direction, visible: visible, keepOut: [orb])
            for key in state.order {
                guard let frame = state.frames[key] else { return XCTFail("\(key) lost") }
                XCTAssertEqual(ScatterLayout.overlapArea(frame, orb), 0, "\(direction) \(key)")
                XCTAssertTrue(visible.contains(frame))
            }
        }
    }

    func testGroupLargerThanTheScreenEndsInside() {
        var state = ScatterLayout.LayoutState()
        let big = CGRect(x: -300, y: 0, width: 4000, height: 1000)
        for i in 1...8 {
            state.cardAdded("c\(i)", size: CGSize(width: 360, height: 200), visible: big, keepOut: [])
        }
        XCTAssertGreaterThan(group(state).width, visible.width)
        for direction in moves {
            var moved = state
            moved.moveAll(direction, visible: visible)
            for key in moved.order {
                guard let frame = moved.frames[key] else { return XCTFail("\(key) lost") }
                XCTAssertTrue(visible.contains(frame), "\(direction) \(key)")
            }
        }
    }

    func testDragStillWorksAfterAMove() {
        var state = compactState()
        state.moveAll(.topLeft, visible: visible)
        let dropped = CGRect(x: 700, y: 400, width: 220, height: 90)
        state.cardDragged("c1", to: dropped, visible: visible)
        XCTAssertEqual(state.frames["c1"], dropped)
        XCTAssertEqual(state.order.last, "c1")
    }

    func testMoveWhileHiddenOrParkedShowsAndMoves() {
        for parked in [false, true] {
            var state = compactState()
            if parked { state.aside() } else { state.hide() }
            state.moveAll(.left, visible: visible)
            XCTAssertFalse(state.hidden)
            XCTAssertEqual(state.mode, .normal)
            XCTAssertEqual(group(state).minX, area.minX, accuracy: 0.001)
            XCTAssertEqual(Set(state.visibleKeys), ["c1", "c2", "c3"])
        }
    }

    func testNonMoveModesAndAnEmptyLayoutAreNoOps() {
        var state = compactState()
        let before = state
        for mode in [LayoutMode.aside, .restore, .hide, .show] {
            state.moveAll(mode, visible: visible)
            XCTAssertEqual(state, before, "\(mode)")
        }
        var empty = ScatterLayout.LayoutState()
        empty.moveAll(.left, visible: visible)
        XCTAssertEqual(empty, ScatterLayout.LayoutState())
    }
}
