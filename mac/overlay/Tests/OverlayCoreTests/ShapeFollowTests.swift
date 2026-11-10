import XCTest
@testable import OverlayCore

final class ShapeFollowTests: XCTestCase {
    private let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
    private let home = CGRect(x: 100, y: 100, width: 800, height: 600)
    private let box = CGRect(x: 150, y: 120, width: 300, height: 30)
    private var anchor: ShapeAnchor { ShapeAnchor(windowID: 1, pid: 7, windowFrame: home, rect: box) }

    private func place(_ frame: CGRect, pid: Int32 = 7, onscreen: Bool = true, above: [CGRect] = [],
                       screens: [CGRect]? = nil) -> CGRect? {
        ShapeFollow.place(anchor, window: WindowSample(pid: pid, frame: frame, isOnscreen: onscreen, above: above),
                          screens: screens ?? [screen])
    }

    // MARK: place

    func testMoveFollowsWindow() {  // a
        XCTAssertEqual(place(CGRect(x: 400, y: 250, width: 800, height: 600)), CGRect(x: 450, y: 270, width: 300, height: 30))
    }

    func testNoMoveKeepsRect() {  // b
        XCTAssertEqual(place(home), box)
    }

    func testResizeBiggerKeepsTopLeftOffset() {  // c
        XCTAssertEqual(place(CGRect(x: 100, y: 100, width: 1000, height: 700)), box)
    }

    func testResizeSmallerPastShapeHides() {  // d
        XCTAssertNil(place(CGRect(x: 100, y: 100, width: 150, height: 600)))
    }

    func testPartlyOffScreenStillShown() {  // e
        XCTAssertEqual(place(CGRect(x: 1150, y: 100, width: 800, height: 600)), CGRect(x: 1200, y: 120, width: 300, height: 30))
    }

    func testMostlyOffScreenHides() {  // f
        XCTAssertNil(place(CGRect(x: 1250, y: 100, width: 800, height: 600)))
    }

    func testFullyOffScreenLeftHides() {  // g
        XCTAssertNil(place(CGRect(x: -900, y: 100, width: 800, height: 600)))
    }

    func testSecondDisplayWithNegativeCoordinates() {  // h
        let screens = [screen, CGRect(x: -1920, y: 0, width: 1920, height: 1080)]
        XCTAssertEqual(place(CGRect(x: -900, y: 100, width: 800, height: 600), screens: screens),
                       CGRect(x: -850, y: 120, width: 300, height: 30))
    }

    func testNotOnscreenHides() {  // i
        XCTAssertNil(place(home, onscreen: false))
    }

    func testMissingWindowHides() {  // j
        XCTAssertNil(ShapeFollow.place(anchor, window: nil, screens: [screen]))
    }

    func testReappearsAfterOffScreenMinimizedOrCovered() {  // k
        XCTAssertNil(place(CGRect(x: 1250, y: 100, width: 800, height: 600)))
        XCTAssertEqual(place(home), box)
        XCTAssertNil(place(home, onscreen: false))
        XCTAssertEqual(place(home), box)
        XCTAssertNil(place(home, above: [CGRect(x: 0, y: 0, width: 400, height: 400)]))
        XCTAssertEqual(place(home, above: []), box)
    }

    func testAnchorPicksFrontmostWindowUnderCentre() {  // l
        let windows: [(id: UInt32, pid: Int32, frame: CGRect)] = [
            (1, 7, CGRect(x: 0, y: 0, width: 400, height: 400)),
            (2, 7, home),
        ]
        XCTAssertEqual(ShapeFollow.anchor(rect: box, windows: windows)?.windowID, 1)
        let right = CGRect(x: 350, y: 120, width: 300, height: 30)
        let anchored = ShapeFollow.anchor(rect: right, windows: windows)
        XCTAssertEqual(anchored, ShapeAnchor(windowID: 2, pid: 7, windowFrame: home, rect: right))
        XCTAssertNil(ShapeFollow.anchor(rect: CGRect(x: 10, y: 2, width: 40, height: 20), windows: [(2, 7, home)]))
        XCTAssertNil(ShapeFollow.anchor(rect: CGRect(x: 0, y: 120, width: 300, height: 30),
                                        windows: [(3, 7, CGRect(x: 100, y: 100, width: 100, height: 600))]))
    }

    func testCoveredCentreHides() {  // m
        XCTAssertNil(place(home, above: [CGRect(x: 0, y: 0, width: 400, height: 400)]))
    }

    func testOccluderElsewhereKeepsShape() {  // n
        XCTAssertEqual(place(home, above: [CGRect(x: 500, y: 0, width: 400, height: 400)]), box)
    }

    func testOccluderMaxEdgeIsExclusive() {  // o
        XCTAssertEqual(place(home, above: [CGRect(x: 0, y: 0, width: 300, height: 135)]), box)
    }

    func testCoveredAfterMoveThenUncovered() {  // p
        let moved = CGRect(x: 400, y: 250, width: 800, height: 600)
        XCTAssertNil(place(moved, above: [CGRect(x: 500, y: 200, width: 300, height: 200)]))
        XCTAssertEqual(place(moved, above: []), CGRect(x: 450, y: 270, width: 300, height: 30))
    }

    func testReusedWindowIDWithOtherPidHides() {  // q
        XCTAssertNil(place(home, pid: 9))
    }

    // MARK: tracker

    private final class FakeScheduler: TickScheduler {
        var starts = 0, stops = 0
        var rates: [Double] = []
        func start(_ tick: @escaping () -> Void) { starts += 1 }
        func stop() { stops += 1 }
        func setRate(hz: Double) { rates.append(hz) }
    }

    private final class FakeSource: WindowSource {
        var windows: [UInt32: WindowSample] = [:]
        func sample(ids: [UInt32]) -> [UInt32: WindowSample] { windows.filter { ids.contains($0.key) } }
    }

    private func makeTracker() -> (ShapeFollowTracker, FakeScheduler, FakeSource, (TimeInterval) -> Void) {
        let scheduler = FakeScheduler()
        let source = FakeSource()
        source.windows[1] = WindowSample(pid: 7, frame: home, isOnscreen: true)
        var clock: TimeInterval = 0
        let tracker = ShapeFollowTracker(source: source, scheduler: scheduler, screens: [screen], now: { clock })
        return (tracker, scheduler, source, { clock = $0 })
    }

    func testNoAnchorsNeverStarts() {  // 3 i
        let (tracker, scheduler, _, _) = makeTracker()
        tracker.setAnchors([:])
        XCTAssertNil(tracker.tick())
        XCTAssertEqual(scheduler.starts, 0)
        XCTAssertEqual(scheduler.stops, 0)
        XCTAssertFalse(tracker.isRunning)
    }

    func testFirstAnchorStartsOnce() {  // 3 ii
        let (tracker, scheduler, _, _) = makeTracker()
        tracker.setAnchors([1: anchor])
        XCTAssertEqual(scheduler.starts, 1)
        XCTAssertEqual(scheduler.rates.last, ShapeFollowTracker.activeHz)
        tracker.setAnchors([1: anchor, 2: anchor])
        XCTAssertEqual(scheduler.starts, 1)
        XCTAssertTrue(tracker.isRunning)
    }

    func testEmptyAnchorsStops() {  // 3 iii
        let (tracker, scheduler, _, _) = makeTracker()
        tracker.setAnchors([1: anchor])
        tracker.setAnchors([:])
        XCTAssertEqual(scheduler.stops, 1)
        XCTAssertFalse(tracker.isRunning)
        tracker.setAnchors([:])
        XCTAssertEqual(scheduler.stops, 1)
    }

    func testUnchangedTickPublishesNothingAndMoveDoes() {  // 3 iv
        let (tracker, _, source, _) = makeTracker()
        tracker.setAnchors([1: anchor])
        XCTAssertNil(tracker.tick())
        source.windows[1]?.frame = CGRect(x: 400, y: 250, width: 800, height: 600)
        XCTAssertEqual(tracker.tick(), FollowUpdate(rects: [1: CGRect(x: 450, y: 270, width: 300, height: 30)]))
        XCTAssertNil(tracker.tick())
        source.windows[1]?.frame.origin.x += 0.2  // below 0.5 pt: no publish
        XCTAssertNil(tracker.tick())
        source.windows[1]?.isOnscreen = false
        XCTAssertEqual(tracker.tick(), FollowUpdate(hidden: [1]))
    }

    func testMissingWindowRemovedAfterTwoSecondsButOffscreenNever() {  // 3 v
        let (tracker, scheduler, source, setClock) = makeTracker()
        source.windows[2] = WindowSample(pid: 7, frame: home, isOnscreen: false)
        let minimized = ShapeAnchor(windowID: 2, pid: 7, windowFrame: home, rect: box)
        let reused = ShapeAnchor(windowID: 1, pid: 8, windowFrame: home, rect: box)
        let closed = ShapeAnchor(windowID: 3, pid: 7, windowFrame: home, rect: box)
        tracker.setAnchors([10: closed, 11: minimized, 12: reused])
        setClock(0)
        XCTAssertEqual(tracker.tick(), FollowUpdate(hidden: [10, 11, 12]))
        setClock(1.9)
        XCTAssertNil(tracker.tick())
        setClock(2.0)
        XCTAssertEqual(tracker.tick(), FollowUpdate(hidden: [11], removed: [10, 12]))
        setClock(10)
        XCTAssertNil(tracker.tick())
        XCTAssertTrue(tracker.isRunning)
        XCTAssertEqual(scheduler.stops, 0)
        // Only a closed window left: removal also stops polling.
        tracker.setAnchors([13: closed])
        _ = tracker.tick()
        setClock(12)
        XCTAssertEqual(tracker.tick(), FollowUpdate(removed: [13]))
        XCTAssertEqual(scheduler.stops, 1)
        XCTAssertFalse(tracker.isRunning)
    }

    func testRateDropsWhenIdleAndReturnsOnChange() {  // 3 vi
        XCTAssertEqual(ShapeFollowTracker.activeHz, 30)
        XCTAssertEqual(ShapeFollowTracker.idleHz, 5)
        let (tracker, scheduler, source, setClock) = makeTracker()
        tracker.setAnchors([1: anchor])
        XCTAssertEqual(scheduler.rates, [30])
        for time in stride(from: 0.5, through: 1.5, by: 0.5) {
            setClock(time)
            XCTAssertNil(tracker.tick())
        }
        XCTAssertEqual(scheduler.rates.last, 30)
        setClock(2.0)
        XCTAssertNil(tracker.tick())
        XCTAssertEqual(scheduler.rates.last, 5)
        setClock(5.0)
        source.windows[1]?.frame.origin.x += 40
        XCTAssertNotNil(tracker.tick())
        XCTAssertEqual(scheduler.rates, [30, 5, 30])
    }
}
