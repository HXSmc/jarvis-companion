import XCTest
@testable import OverlayCore

// A 1440x900 screen with a 25 pt menu bar, no Dock: the visible frame in top-left points.
private let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
private let visible = CGRect(x: 0, y: 25, width: 1440, height: 875)
private let area = ScatterLayout.insetArea(visible)
private let moves: [LayoutMode] = [.left, .right, .up, .down, .topLeft, .topRight, .bottomLeft, .bottomRight, .otherSide]

final class HideMoveTests: XCTestCase {

    private func decode(_ line: String) -> OverlayEvent {
        OverlayEvent.decode(line: Data(line.utf8))
    }

    /// Three cards placed in a small region in the middle of the screen, the second one pinned.
    private func compactState() -> ScatterLayout.LayoutState {
        var state = ScatterLayout.LayoutState()
        let region = CGRect(x: 400, y: 250, width: 760, height: 460)
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
