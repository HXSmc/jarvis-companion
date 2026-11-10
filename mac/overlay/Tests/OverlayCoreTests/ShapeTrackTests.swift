import XCTest
import CoreText
@testable import OverlayCore

final class ShapeTrackTests: XCTestCase {
    private let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
    private let home = CGRect(x: 100, y: 100, width: 800, height: 600)

    private func window(_ frame: CGRect? = nil, pid: Int32 = 7, above: [CGRect] = []) -> WindowSample {
        WindowSample(pid: pid, frame: frame ?? home, isOnscreen: true, above: above)
    }

    private func elementAnchor(_ fit: ElementFit, element: CGRect, box: CGRect, text: String = "",
                               handle: Int = 1, windowID: UInt32 = 1, pid: Int32 = 7) -> ElementAnchor {
        ElementAnchor(fit: fit, window: ShapeAnchor(windowID: windowID, pid: pid, windowFrame: home, rect: box),
                      handle: handle, elementFrame: element, text: text)
    }

    private func read(_ frame: CGRect, text: String = "", windowFrame: CGRect? = nil) -> ElementRead {
        ElementRead(status: .ok, frame: frame, text: text, windowFrame: windowFrame ?? home)
    }

    // MARK: refind (2a, 2b)

    func testRefindPicksFreshObservationPadded() {  // 2a
        let observations: [(text: String, rect: CGRect)] = [
            ("127.0.0.1", CGRect(x: 500, y: 12, width: 80, height: 16)),
            ("Coursework", CGRect(x: 90, y: 60, width: 90, height: 15)),
        ]
        XCTAssertEqual(ShapeTrack.refind(label: "127.0.0.1", stale: CGRect(x: 250, y: 10, width: 80, height: 20),
                                         observations: observations),
                       CGRect(x: 496, y: 8, width: 88, height: 24))
    }

    func testRefindNearestAndNoMatch() {  // 2b
        let stale = CGRect(x: 250, y: 10, width: 80, height: 20)
        let observations: [(text: String, rect: CGRect)] = [
            ("Inbox", CGRect(x: 900, y: 10, width: 40, height: 16)),
            ("Inbox", CGRect(x: 300, y: 40, width: 40, height: 16)),
        ]
        XCTAssertEqual(ShapeTrack.refind(label: "Inbox", stale: stale, observations: observations),
                       CGRect(x: 296, y: 36, width: 48, height: 24))
        XCTAssertNil(ShapeTrack.refind(label: "Drafts", stale: stale, observations: observations))
    }

    func testTolerantTextMatch() {  // 2b
        XCTAssertTrue(ShapeTrack.textMatches("Smart  Search", "smart search"))
        XCTAssertTrue(ShapeTrack.textMatches("Open the long settin", "Open the long settings page"))
        XCTAssertTrue(ShapeTrack.textMatches("127.0.0.1", "l27.0.0.1"))
        XCTAssertTrue(ShapeTrack.textMatches("127.0.0.1", "127.O.0.1"))
        XCTAssertFalse(ShapeTrack.textMatches("abc", "abd"))
        XCTAssertFalse(ShapeTrack.textMatches("Inbox", "Outbox"))
        XCTAssertFalse(ShapeTrack.textMatches("", ""))
    }

    func testOnlyOneRefindAttemptAndRecheckMissUsesLookup() async {  // 2b, 3 ix
        var calls: [String] = []
        let none: CGRect? = await ShapeTrack.reresolve(
            hasCheck: false,
            recheck: { calls.append("recheck"); return nil },
            lookup: { calls.append("lookup"); return nil },
            refind: { calls.append("refind"); return nil })
        XCTAssertNil(none)
        XCTAssertEqual(calls, ["refind"])
        calls = []
        let fresh = CGRect(x: 1, y: 2, width: 3, height: 4)
        let hit: CGRect? = await ShapeTrack.reresolve(
            hasCheck: true,
            recheck: { calls.append("recheck"); return nil },
            lookup: { calls.append("lookup"); return fresh },
            refind: { calls.append("refind"); return nil })
        XCTAssertEqual(hit, fresh)
        XCTAssertEqual(calls, ["recheck", "lookup"])
    }

    // MARK: acceptElement (2c)

    func testAcceptElement() {
        let box = CGRect(x: 496, y: 44, width: 92, height: 22)
        let snap = ShapeTrack.acceptElement(box: box, elementFrame: CGRect(x: 300, y: 40, width: 400, height: 30),
                                            role: "AXTextField", value: "127.0.0.1", label: "", shapeLabel: "127.0.0.1")
        XCTAssertEqual(snap?.fit, .snap)
        XCTAssertEqual(snap?.rect, CGRect(x: 296, y: 36, width: 408, height: 38))
        let offset = ShapeTrack.acceptElement(box: box, elementFrame: CGRect(x: 490, y: 40, width: 110, height: 30),
                                              role: "AXStaticText", value: "", label: "", shapeLabel: "127.0.0.1")
        XCTAssertEqual(offset?.fit, .offset)
        XCTAssertEqual(offset?.rect, box)
        XCTAssertNil(ShapeTrack.acceptElement(box: box, elementFrame: screen, role: "AXGroup",
                                              value: "", label: "", shapeLabel: "127.0.0.1"))
    }

    // MARK: placeElement (2d, 2e, 2f, 2k, 2l)

    private func place(_ anchor: ElementAnchor, _ element: ElementRead, window sample: WindowSample? = nil,
                       clip: CGRect? = CGRect(x: 100, y: 140, width: 800, height: 560), wasShown: Bool = true) -> CGRect? {
        ShapeTrack.placeElement(anchor: anchor, element: element, elementReadWindowFrame: element.windowFrame,
                                clip: clip, window: sample ?? window(), screens: [screen], wasShown: wasShown)
    }

    func testElementFollowsScrollAndHidesOutOfClip() {  // 2d
        let anchor = elementAnchor(.snap, element: CGRect(x: 300, y: 400, width: 400, height: 30),
                                   box: CGRect(x: 296, y: 396, width: 408, height: 38))
        XCTAssertEqual(place(anchor, read(CGRect(x: 300, y: 300, width: 400, height: 30))),
                       CGRect(x: 296, y: 296, width: 408, height: 38))
        XCTAssertNil(place(anchor, read(CGRect(x: 300, y: 110, width: 400, height: 30))))
        XCTAssertEqual(place(anchor, read(CGRect(x: 300, y: 300, width: 400, height: 30)), wasShown: false),
                       CGRect(x: 296, y: 296, width: 408, height: 38))
    }

    func testElementInvalidAndTextChange() {  // 2e
        let frame = CGRect(x: 300, y: 400, width: 400, height: 30)
        let offset = elementAnchor(.offset, element: frame, box: CGRect(x: 310, y: 404, width: 60, height: 22), text: "Inbox")
        XCTAssertNil(place(offset, ElementRead(status: .invalid)))
        XCTAssertNotNil(place(offset, read(frame, text: "Inbox")))
        XCTAssertNil(place(offset, read(frame, text: "Drafts")))
        let snap = elementAnchor(.snap, element: frame, box: frame.insetBy(dx: -4, dy: -4), text: "127.0.0.1")
        XCTAssertEqual(place(snap, read(frame, text: "127.0.0.1/a")), frame.insetBy(dx: -4, dy: -4))
    }

    func testInWindowLayoutChange() {  // 2f
        let anchor = elementAnchor(.offset, element: CGRect(x: 300, y: 400, width: 100, height: 30),
                                   box: CGRect(x: 310, y: 404, width: 60, height: 22))
        XCTAssertEqual(place(anchor, read(CGRect(x: 450, y: 400, width: 100, height: 30)))?.minX, 460)
    }

    func testElementKeepsItsOwnWindowBehindAnother() {  // 2k
        let front = CGRect(x: 300, y: 100, width: 800, height: 600)
        let windows: [ShapeTrack.Window] = [(2, 9, front), (1, 7, home)]
        let own = ShapeTrack.windowFor(elementWindowFrame: home, pid: 7, windows: windows)
        XCTAssertEqual(own?.id, 1)
        XCTAssertEqual(own?.pid, 7)
        let box = CGRect(x: 496, y: 396, width: 88, height: 24)
        XCTAssertEqual(ShapeFollow.anchor(rect: box, windows: windows)?.pid, 9)  // the hit-test would pick the cover
        let anchor = elementAnchor(.offset, element: CGRect(x: 490, y: 390, width: 110, height: 40), box: box)
        XCTAssertNil(place(anchor, read(CGRect(x: 490, y: 390, width: 110, height: 40)), window: window(above: [front])))
    }

    func testNoDoubleMotion() {  // 2l
        let anchor = elementAnchor(.snap, element: CGRect(x: 300, y: 400, width: 400, height: 30),
                                   box: CGRect(x: 296, y: 396, width: 408, height: 38))
        let moved = CGRect(x: 150, y: 100, width: 800, height: 600)
        let expected = CGRect(x: 346, y: 396, width: 408, height: 38)
        XCTAssertEqual(place(anchor, read(CGRect(x: 300, y: 400, width: 400, height: 30)), window: window(moved), clip: nil),
                       expected)
        XCTAssertEqual(place(anchor, read(CGRect(x: 350, y: 400, width: 400, height: 30), windowFrame: moved),
                             window: window(moved), clip: nil), expected)
    }

    // MARK: bands (2g)

    func testPlaceBand() {
        let marks = (10...40).map { (line: $0, midY: CGFloat(100 + 20 * ($0 - 10))) }
        let clip = CGRect(x: 100, y: 0, width: 800, height: 900)
        XCTAssertEqual(ShapeTrack.placeBand(lines: 20...21, marks: marks, x: 140, width: 600, clip: clip),
                       CGRect(x: 140, y: 290, width: 600, height: 40))
        let scrolled = marks.map { (line: $0.line, midY: $0.midY - 60) }
        XCTAssertEqual(ShapeTrack.placeBand(lines: 20...21, marks: scrolled, x: 140, width: 600, clip: clip),
                       CGRect(x: 140, y: 230, width: 600, height: 40))
        XCTAssertNil(ShapeTrack.placeBand(lines: 50...51, marks: marks, x: 140, width: 600, clip: clip))
        // Band (140,290,600,40): a clip ending at y 302 keeps 30% of it.
        XCTAssertNil(ShapeTrack.placeBand(lines: 20...21, marks: marks, x: 140, width: 600,
                                          clip: CGRect(x: 100, y: 0, width: 800, height: 302)))
    }

    func testGutterMarksAndScrollBarSignal() {
        let digits = (1...6).map { (line: $0, centre: CGPoint(x: 120, y: CGFloat(100 + 20 * $0)), right: CGFloat(130)) }
            + [(line: 99, centre: CGPoint(x: 600, y: 100), right: CGFloat(610))]
        let gutter = ShapeTrack.gutterMarks(digits: digits, window: home)
        XCTAssertEqual(Set(gutter?.marks.map(\.line) ?? []), Set(1...6))
        XCTAssertEqual(gutter?.right, 130)
        XCTAssertNil(ShapeTrack.gutterMarks(digits: Array(digits.prefix(4)), window: home))
        XCTAssertEqual(ShapeTrack.lineNumber("042"), 42)
        XCTAssertNil(ShapeTrack.lineNumber("12a"))
        XCTAssertTrue(ShapeTrack.scrolled(previous: 0.2, current: 0.21))
        XCTAssertFalse(ShapeTrack.scrolled(previous: 0.2, current: 0.2))
        XCTAssertFalse(ShapeTrack.scrolled(previous: nil, current: 0.2))
    }

    // MARK: add time (2h, 3 ix)

    func testAnchorBuiltFromReresolvedRect() {  // 2h
        let stale = CGRect(x: 250, y: 10, width: 80, height: 20)
        let fresh = CGRect(x: 496, y: 8, width: 88, height: 24)
        let frame = CGRect(x: 100, y: 0, width: 800, height: 600)
        guard case .anchored(let anchor) = ShapeTrack.placement(fresh: fresh, stale: stale, windows: [(1, 7, frame)],
                                                                screens: [screen])
        else { return XCTFail("not anchored") }
        XCTAssertEqual(anchor.rect, fresh)
        XCTAssertNotEqual(anchor.rect, stale)
        XCTAssertEqual(anchor.windowFrame, frame)
        XCTAssertEqual(ShapeFollow.place(anchor, window: window(frame.offsetBy(dx: 50, dy: 0)), screens: [screen]),
                       CGRect(x: 546, y: 8, width: 88, height: 24))
    }

    func testFallbackDrawsOriginalRectAndGoneDrawsNothing() {  // 3 ix
        let stale = CGRect(x: 150, y: 120, width: 300, height: 30)
        XCTAssertEqual(ShapeTrack.placement(fresh: nil, stale: stale, windows: [(1, 7, home)], screens: [screen]),
                       .anchored(ShapeAnchor(windowID: 1, pid: 7, windowFrame: home, rect: stale)))
        XCTAssertEqual(ShapeTrack.placement(fresh: nil, stale: stale, windows: [], screens: [screen]), .unanchored(stale))
        let offscreen = CGRect(x: -900, y: 100, width: 800, height: 600)
        XCTAssertEqual(ShapeTrack.placement(fresh: nil, stale: CGRect(x: -850, y: 120, width: 300, height: 30),
                                            windows: [(1, 7, offscreen)], screens: [screen]), .gone)
    }

    func testMatchWindow() {
        let padded = home.insetBy(dx: -4, dy: -4)
        XCTAssertEqual(ShapeTrack.matchWindow(stale: padded.offsetBy(dx: 30, dy: 0), windows: [(1, 7, home)]), padded)
        XCTAssertEqual(ShapeTrack.matchWindow(stale: padded.offsetBy(dx: 500, dy: 0), windows: [(1, 7, home)]), padded)
        XCTAssertNil(ShapeTrack.matchWindow(stale: CGRect(x: 120, y: 120, width: 88, height: 24), windows: [(1, 7, home)]))
    }

    // MARK: hysteresis (2i), tier C (2j), ordering (2m)

    func testHysteresisInTheTracker() {  // 2i, through the real tick path
        let rig = makeRig()
        rig.tracker.setTracks([1: windowBox()])
        // Box (150,120,300,30); its on-screen share is (1440 - minX) / 300 once the window moves right.
        func share(_ value: CGFloat) -> FollowUpdate? {
            let minX = 1440 - value * 300
            rig.windows.windows[1]?.frame.origin.x = 100 + (minX - 150)
            return rig.tracker.tick()
        }
        XCTAssertNotNil(share(0.48)?.rects[1])  // shown, stays shown at 0.48
        XCTAssertNotNil(share(0.46)?.rects[1])
        XCTAssertEqual(share(0.44)?.hidden, [1])  // below 0.45: hides
        XCTAssertNil(share(0.47))  // hidden: no blink back between 0.45 and 0.55
        XCTAssertNil(share(0.52))
        XCTAssertNotNil(share(0.56)?.rects[1])  // from 0.55: shows again
        XCTAssertNotNil(share(0.50)?.rects[1])  // and stays shown at 0.50
    }

    // MARK: content check (1b)

    /// 64x64 text-like luma: 4 px dark text rows every 8 px with word gaps.
    private func textLike(shift: Int = 0) -> [UInt8] {
        (0..<64 * 64).map { index in
            let x = index % 64, y = index / 64
            return ((y + shift) / 4) % 2 == 0 && (x / 6) % 3 != 2 ? 40 : 230
        }
    }

    private func grid(_ luma: [UInt8]) -> [UInt8] { ShapeTrack.lumaGrid(luma: luma, width: 64, height: 64) }

    func testContentGrid() {
        let base = grid(textLike())
        XCTAssertEqual(base.count, 256)
        XCTAssertFalse(ShapeTrack.contentMoved(reference: base, current: base))  // identical
        var caret = textLike()
        for y in 16..<48 { caret[y * 64 + 30] = 0; caret[y * 64 + 31] = 0 }
        XCTAssertFalse(ShapeTrack.contentMoved(reference: base, current: grid(caret)))  // caret-sized change
        let tinted = textLike().map { UInt8(min(255, Int($0) + 20)) }
        XCTAssertFalse(ShapeTrack.contentMoved(reference: base, current: grid(tinted)))  // hover tint
        XCTAssertTrue(ShapeTrack.contentMoved(reference: base, current: grid(textLike(shift: 4))))  // scrolled half a line
        XCTAssertTrue(ShapeTrack.contentMoved(reference: base, current: grid(textLike(shift: 12))))
        XCTAssertEqual(ShapeTrack.lumaGrid(luma: [0, 0], width: 1, height: 2), [])
    }

    func testTierCRules() {  // 2j (pure part; the tracker part is testWindowBoxHidesForGoodOnScrollOrResize)
        XCTAssertTrue(ShapeTrack.scrollHits(CGPoint(x: 300, y: 300), window: window()))
        XCTAssertFalse(ShapeTrack.scrollHits(CGPoint(x: 1000, y: 300), window: window()))
        XCTAssertFalse(ShapeTrack.scrollHits(CGPoint(x: 300, y: 300), window: window(above: [home])))
        XCTAssertTrue(ShapeTrack.resized(from: home, to: CGRect(x: 100, y: 100, width: 803, height: 600)))
        XCTAssertFalse(ShapeTrack.resized(from: home, to: CGRect(x: 100, y: 100, width: 801, height: 600)))
    }

    func testAddOrder() {  // 2m
        var order = ShapeTrack.AddOrder()
        let one = order.begin(), two = order.begin(), three = order.begin()
        order.clear()
        XCTAssertFalse(order.canCommit(one))
        XCTAssertFalse(order.canCommit(two))
        XCTAssertFalse(order.canCommit(three))
        XCTAssertFalse(order.commit(one))
        let four = order.begin(), five = order.begin(), six = order.begin()
        XCTAssertTrue(order.commit(four))
        XCTAssertFalse(order.commit(six))  // not before five
        XCTAssertTrue(order.commit(five))
        XCTAssertTrue(order.commit(six))
    }

    // MARK: tracker with fakes (3)

    private final class FakeScheduler: TickScheduler {
        var starts = 0, stops = 0
        var rates: [Double] = []
        func start(_ tick: @escaping () -> Void) { starts += 1 }
        func stop() { stops += 1 }
        func setRate(hz: Double) { rates.append(hz) }
    }

    private final class FakeWindows: WindowSource {
        var windows: [UInt32: WindowSample] = [:]
        func sample(ids: [UInt32]) -> [UInt32: WindowSample] { windows.filter { ids.contains($0.key) } }
    }

    private final class FakeElements: ElementSource {
        var reads: [(pid: Int32, handles: [Int: UInt32])] = []
        var refinds: [(window: UInt32, lines: [Int: ClosedRange<Int>])] = []
        var checks: [[Int: CGRect]] = []
        func checkContent(_ rects: [Int: CGRect]) { checks.append(rects) }
        var monitor: [Bool] = []
        var held: Set<Int> = []
        func read(pid: Int32, handles: [Int: UInt32]) { reads.append((pid, handles)) }
        func refindBands(window: UInt32, lines: [Int: ClosedRange<Int>]) { refinds.append((window, lines)) }
        func setScrollMonitor(_ on: Bool) { monitor.append(on) }
        func release(_ handles: Set<Int>) { held.subtract(handles) }
    }

    private struct Rig {
        let tracker: ShapeFollowTracker
        let scheduler: FakeScheduler
        let windows: FakeWindows
        let elements: FakeElements
        let setClock: (TimeInterval) -> Void
    }

    private func makeRig() -> Rig {
        let scheduler = FakeScheduler(), windows = FakeWindows(), elements = FakeElements()
        windows.windows[1] = WindowSample(pid: 7, frame: home, isOnscreen: true)
        var clock: TimeInterval = 0
        let tracker = ShapeFollowTracker(source: windows, scheduler: scheduler, screens: [screen],
                                         elements: elements, now: { clock })
        return Rig(tracker: tracker, scheduler: scheduler, windows: windows, elements: elements, setClock: { clock = $0 })
    }

    private let elementFrame = CGRect(x: 300, y: 400, width: 400, height: 30)
    private var elementBox: CGRect { elementFrame.insetBy(dx: -4, dy: -4) }

    private func element(handle: Int = 1, windowID: UInt32 = 1, pid: Int32 = 7) -> TrackAnchor {
        .element(elementAnchor(.snap, element: elementFrame, box: elementBox, handle: handle, windowID: windowID, pid: pid))
    }

    private let windowRect = CGRect(x: 150, y: 120, width: 300, height: 30)

    private func windowBox(windowID: UInt32 = 1, pid: Int32 = 7, rect: CGRect? = nil, scrollBar: Int? = nil) -> TrackAnchor {
        .window(ShapeAnchor(windowID: windowID, pid: pid, windowFrame: home, rect: rect ?? windowRect, scrollBar: scrollBar))
    }

    private func band(scrollBar: Int? = nil, lines: ClosedRange<Int> = 20...21) -> TrackAnchor {
        .band(BandAnchor(window: ShapeAnchor(windowID: 1, pid: 7, windowFrame: home,
                                             rect: CGRect(x: 140, y: 290, width: 600, height: 40), scrollBar: scrollBar),
                         lines: lines))
    }

    func testNoTracksCostsNothing() {  // 3 i
        let rig = makeRig()
        rig.tracker.setTracks([:])
        XCTAssertNil(rig.tracker.tick())
        XCTAssertEqual(rig.elements.reads.count, 0)
        XCTAssertEqual(rig.scheduler.starts, 0)
        XCTAssertEqual(rig.elements.monitor, [])
    }

    func testReadEveryThirdTickActiveAndEveryTickIdle() {  // 3 ii
        let rig = makeRig()
        rig.tracker.setTracks([1: element()])
        for _ in 1...9 {
            _ = rig.tracker.tick()
            rig.tracker.receive(pid: 7, reads: [:])
        }
        XCTAssertEqual(rig.elements.reads.count, 3)
        XCTAssertEqual(rig.elements.monitor, [])  // element boxes need no scroll monitor
        rig.setClock(3)
        _ = rig.tracker.tick()  // settles to idleHz
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.idleHz)
        rig.tracker.receive(pid: 7, reads: [:])
        let before = rig.elements.reads.count
        for step in 1...3 {
            rig.setClock(3 + 0.2 * Double(step))
            _ = rig.tracker.tick()
            rig.tracker.receive(pid: 7, reads: [:])
        }
        XCTAssertEqual(rig.elements.reads.count, before + 3)
    }

    func testOnlyEightElementBoxesPolled() {  // 3 iii
        let rig = makeRig()
        rig.elements.held = Set(1...9)
        rig.tracker.setTracks(Dictionary(uniqueKeysWithValues: (1...9).map { ($0, element(handle: $0)) }))
        XCTAssertEqual(rig.elements.monitor, [true])  // the 9th is window-anchored
        for _ in 1...3 { _ = rig.tracker.tick() }
        XCTAssertEqual(rig.elements.reads.count, 1)
        XCTAssertEqual(Set(rig.elements.reads[0].handles.keys), Set(1...8))
        rig.windows.windows[1]?.frame.origin.x += 10
        let update = rig.tracker.tick()
        XCTAssertEqual(update?.rects[9], elementBox.offsetBy(dx: 10, dy: 0))
    }

    func testNewTrackWhileIdleGoesActive() {  // 3 iv
        let rig = makeRig()
        rig.tracker.setTracks([1: windowBox()])
        rig.setClock(3)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.idleHz)
        rig.tracker.setTracks([1: windowBox(), 2: element()])
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.activeHz)
    }

    func testEmptyTracksStopsEverythingAndReleases() {  // 3 v
        let rig = makeRig()
        rig.elements.held = [1]
        rig.tracker.setTracks([1: element(handle: 1), 2: windowBox()])
        XCTAssertEqual(rig.elements.monitor, [true])
        rig.tracker.setTracks([:])
        XCTAssertEqual(rig.scheduler.stops, 1)
        XCTAssertEqual(rig.elements.monitor, [true, false])
        XCTAssertEqual(rig.elements.held, [])
    }

    func testBandHidesOnScrollAndIsRefoundAfterQuiet() {  // 3 vi
        let rig = makeRig()
        rig.tracker.setTracks([1: band()])
        XCTAssertNil(rig.tracker.tick())
        rig.tracker.scrollWheel(at: CGPoint(x: 400, y: 400))
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(hidden: [1]))
        rig.setClock(0.1)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.refinds.count, 0)
        rig.setClock(0.3)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.refinds.count, 1)
        XCTAssertEqual(rig.elements.refinds[0].lines, [1: 20...21])
        let fresh = CGRect(x: 140, y: 230, width: 600, height: 40)
        rig.tracker.receiveBands(window: 1, rects: [1: fresh], windowFrame: home)
        XCTAssertEqual(rig.tracker.tick()?.rects[1], fresh)
    }

    func testBandRefindsCappedAtTwoPerSecond() {  // 3 vi: bursts with quiet gaps
        let rig = makeRig()
        rig.tracker.setTracks([1: band()])
        _ = rig.tracker.tick()
        // Ticks every 1/32 s (exact in binary); a scroll every 10 ticks, so 8-tick quiet gaps occur
        // every 10 ticks. Without the 0.5 s cap that would be a re-find every 10 ticks.
        var refindTicks: [Int] = []
        for step in 0..<64 {
            rig.setClock(Double(step) / 32)
            if step % 10 == 0 { rig.tracker.scrollWheel(at: CGPoint(x: 400, y: 400)) }
            let before = rig.elements.refinds.count
            _ = rig.tracker.tick()
            if rig.elements.refinds.count > before {
                refindTicks.append(step)
                rig.tracker.receiveBands(window: 1, rects: [1: CGRect(x: 140, y: 290, width: 600, height: 40)], windowFrame: home)
            }
        }
        XCTAssertEqual(refindTicks, [8, 28, 48])  // >= 16 ticks (0.5 s) apart: at most 2 per second
    }

    func testNoRefindWhenNoBandIsLeft() {  // item 2
        let rig = makeRig()
        rig.tracker.setTracks([1: band()])
        _ = rig.tracker.tick()
        rig.tracker.scrollWheel(at: CGPoint(x: 400, y: 400))
        _ = rig.tracker.tick()
        rig.tracker.setTracks([2: windowBox()])
        rig.setClock(0.5)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.refinds.count, 0)
    }

    func testScrollBarValueChangeHidesBand() {  // 3 vi (scroll-bar signal)
        let rig = makeRig()
        rig.elements.held = [5]
        rig.tracker.setTracks([1: band(scrollBar: 5)])
        XCTAssertEqual(rig.elements.checks.count, 0)
        for _ in 1...3 { _ = rig.tracker.tick() }
        XCTAssertEqual(rig.elements.checks.count, 1)  // checked at placement even with a scroll bar
        XCTAssertEqual(rig.elements.reads.last?.handles, [5: 1])
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 0.2)])
        XCTAssertNil(rig.tracker.tick())
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 0.3)])
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(hidden: [1]))
    }

    func testTimeoutKeepsLastRectThenHidesNeverRemoves() {  // 3 vii
        let rig = makeRig()
        rig.tracker.setTracks([1: element()])
        rig.tracker.receive(pid: 7, reads: [1: read(elementFrame.offsetBy(dx: 0, dy: -50))])
        XCTAssertEqual(rig.tracker.tick()?.rects[1], elementBox.offsetBy(dx: 0, dy: -50))
        rig.tracker.receive(pid: 7, reads: [1: ElementRead(status: .timeout)])
        rig.setClock(0.9)
        XCTAssertNil(rig.tracker.tick())  // kept
        rig.setClock(1.0)
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(hidden: [1]))
        rig.setClock(10)
        XCTAssertNil(rig.tracker.tick())
        XCTAssertTrue(rig.tracker.isRunning)
    }

    func testInvalidElementRemovedAfterTwoSeconds() {  // 2e (removal via the tracker)
        let rig = makeRig()
        rig.tracker.setTracks([1: element()])
        rig.tracker.receive(pid: 7, reads: [1: ElementRead(status: .invalid)])
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(hidden: [1]))
        rig.setClock(1.9)
        XCTAssertNil(rig.tracker.tick())
        rig.setClock(2.0)
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(removed: [1]))
        XCTAssertFalse(rig.tracker.isRunning)
    }

    func testHungAppDelaysNobodyElse() {  // 3 viii
        let rig = makeRig()
        let other = CGRect(x: 1000, y: 100, width: 400, height: 600)
        rig.windows.windows[2] = WindowSample(pid: 9, frame: other, isOnscreen: true)
        rig.elements.held = [1, 2]
        rig.tracker.setTracks([1: element(handle: 1), 2: element(handle: 2, windowID: 2, pid: 9), 3: windowBox()])
        for step in 1...30 {
            rig.setClock(Double(step) / 30)
            rig.windows.windows[1]?.frame.origin.x = 100 + CGFloat(step)
            let update = rig.tracker.tick()
            XCTAssertEqual(update?.rects[3]?.minX, 150 + CGFloat(step))  // tier C box: every tick
            if step % 3 == 0 {
                rig.tracker.receive(pid: 9, reads: [2: read(CGRect(x: 1100, y: 400 + CGFloat(step), width: 200, height: 30),
                                                             windowFrame: other)])
            }
        }
        XCTAssertEqual(rig.elements.reads.filter { $0.pid == 7 }.count, 1)  // never a second batch while one is out
        XCTAssertGreaterThan(rig.elements.reads.filter { $0.pid == 9 }.count, 5)
        rig.setClock(1.2)
        let update = rig.tracker.tick()
        XCTAssertNotNil(update?.rects[2])
        XCTAssertNil(update?.rects[1])
    }

    func testWindowBoxHidesForGoodOnScrollOrResize() {  // 2j
        let rig = makeRig()
        rig.tracker.setTracks([1: windowBox()])
        XCTAssertNil(rig.tracker.tick())
        rig.tracker.scrollWheel(at: CGPoint(x: 1200, y: 400))  // outside the window
        XCTAssertNil(rig.tracker.tick())
        rig.tracker.scrollWheel(at: CGPoint(x: 400, y: 400))
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(removed: [1]))  // hidden for good: removed
        XCTAssertFalse(rig.tracker.isRunning)  // the last box gone: no timer, no monitor
        XCTAssertEqual(rig.elements.monitor, [true, false])
        rig.setClock(5)
        XCTAssertNil(rig.tracker.tick())  // never back at the original frame
        XCTAssertEqual(rig.tracker.tick(), nil)

        let resize = makeRig()
        resize.tracker.setTracks([1: windowBox()])
        resize.windows.windows[1]?.frame.size.width += 1
        XCTAssertNil(resize.tracker.tick())
        resize.windows.windows[1]?.frame.size.width += 2  // 3 pt now
        XCTAssertEqual(resize.tracker.tick(), FollowUpdate(removed: [1]))
        resize.windows.windows[1]?.frame = home
        XCTAssertNil(resize.tracker.tick())
    }

    // MARK: round 1

    func testWindowBoxHidesOnScrollBarChange() {  // 1a: keyboard / scroll-bar scrolls
        let rig = makeRig()
        rig.elements.held = [5]
        rig.tracker.setTracks([1: windowBox(scrollBar: 5)])
        for _ in 1...3 { _ = rig.tracker.tick() }
        XCTAssertEqual(rig.elements.reads.last?.handles, [5: 1])
        XCTAssertEqual(rig.elements.checks.count, 1)  // content-checked too (round 2)
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 0.2)])
        XCTAssertNil(rig.tracker.tick())
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 0.25)])
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(removed: [1]))
        rig.setClock(5)
        XCTAssertNil(rig.tracker.tick())  // for good
    }

    func testContentCheckHidesWindowBoxWhenContentMoves() {  // 1b
        let rig = makeRig()
        rig.tracker.setTracks([1: windowBox()])
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.checks, [[1: windowRect]])
        let base = grid(textLike())
        rig.tracker.receiveContent([1: base], rects: [1: windowRect])  // reference
        rig.setClock(0.5)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.checks.count, 1)  // once per second
        rig.setClock(1)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.checks.count, 2)
        var caret = textLike()
        for y in 16..<48 { caret[y * 64 + 30] = 0 }
        rig.tracker.receiveContent([1: grid(caret)], rects: [1: windowRect])
        XCTAssertNil(rig.tracker.tick())  // a caret blink keeps the box
        rig.setClock(2)
        _ = rig.tracker.tick()
        // Moved while the capture ran: ignored.
        rig.tracker.receiveContent([1: grid(textLike(shift: 4))], rects: [1: windowRect.offsetBy(dx: 9, dy: 0)])
        XCTAssertNil(rig.tracker.tick())
        rig.setClock(3)
        _ = rig.tracker.tick()
        rig.tracker.receiveContent([1: grid(textLike(shift: 4))], rects: [1: windowRect])
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(removed: [1]))
        rig.setClock(4)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.checks.count, 4)  // a retired box is not checked again
    }

    func testContentCheckAtMostFourBoxesAndBandRefinds() {  // 1b
        let rig = makeRig()
        var tracks: [Int: TrackAnchor] = [:]
        for key in 1...5 { tracks[key] = windowBox(rect: windowRect.offsetBy(dx: 0, dy: CGFloat(40 * key))) }
        tracks[6] = band()
        rig.tracker.setTracks(tracks)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.checks.last?.count, 4)
        XCTAssertEqual(Set(rig.elements.checks.last.map { Array($0.keys) } ?? []), [1, 2, 3, 4])
        rig.tracker.receiveContent([:], rects: [:])
        rig.setClock(1)
        _ = rig.tracker.tick()
        XCTAssertEqual(Set(rig.elements.checks.last.map { Array($0.keys) } ?? []), [5, 6, 1, 2])  // least recently checked first
        let bandRect = CGRect(x: 140, y: 290, width: 600, height: 40)
        rig.tracker.receiveContent([6: grid(textLike())], rects: [6: bandRect])
        rig.setClock(2)
        _ = rig.tracker.tick()
        rig.tracker.receiveContent([:], rects: [:])
        rig.setClock(3)
        _ = rig.tracker.tick()
        XCTAssertTrue(rig.elements.checks.last?.keys.contains(6) ?? false)
        rig.tracker.receiveContent([6: grid(textLike(shift: 4))], rects: [6: bandRect])
        XCTAssertEqual(rig.tracker.tick()?.hidden.contains(6), true)
        rig.setClock(3.3)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.refinds.count, 1)  // the band is re-found, not dropped
    }

    func testElementReadBurstAfterInWindowMove() {  // item 8
        let rig = makeRig()
        rig.tracker.setTracks([1: element()])
        _ = rig.tracker.tick()  // tick 1, no read
        rig.tracker.receive(pid: 7, reads: [1: read(elementFrame)])
        rig.tracker.receive(pid: 7, reads: [1: read(elementFrame.offsetBy(dx: 0, dy: -10))])
        var burst = 0
        for step in 1...7 {
            rig.setClock(0.05 * Double(step))
            let before = rig.elements.reads.count
            _ = rig.tracker.tick()
            burst += rig.elements.reads.count - before
            rig.tracker.receive(pid: 7, reads: [:])
        }
        XCTAssertEqual(burst, 7)  // every tick for 400 ms
        let before = rig.elements.reads.count
        for step in 1...6 {
            rig.setClock(0.5 + 0.03 * Double(step))
            _ = rig.tracker.tick()
            rig.tracker.receive(pid: 7, reads: [:])
        }
        XCTAssertEqual(rig.elements.reads.count - before, 2)  // then every 3rd tick again
        // A window drag moves element and window together: no burst.
        rig.setClock(1)
        rig.tracker.receive(pid: 7, reads: [1: read(elementFrame.offsetBy(dx: 30, dy: -10),
                                                    windowFrame: home.offsetBy(dx: 30, dy: 0))])
        let afterDrag = rig.elements.reads.count
        rig.setClock(1.05)
        _ = rig.tracker.tick()  // tick 15: a multiple of 3
        rig.tracker.receive(pid: 7, reads: [:])
        rig.setClock(1.1)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.reads.count - afterDrag, 1)
    }

    func testScrollWakesTrackerFromIdleAtOnce() {  // item 8
        let rig = makeRig()
        rig.elements.held = [5]
        rig.tracker.setTracks([1: windowBox(), 2: band(scrollBar: 5)])
        rig.setClock(3)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.idleHz)
        rig.tracker.scrollWheel(at: CGPoint(x: 1300, y: 400))  // not over a tracked window
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.idleHz)
        rig.tracker.scrollWheel(at: CGPoint(x: 400, y: 400))
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.activeHz)  // before any tick
        rig.setClock(3.1)
        _ = rig.tracker.tick()
        rig.setClock(6)
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.idleHz)
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 0.1)])
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 0.2)])
        XCTAssertEqual(rig.scheduler.rates.last, ShapeFollowTracker.activeHz)
    }

    func testWholeWindowBoxNeedsNoMonitorOrContentCheck() {  // item 9
        let rig = makeRig()
        rig.tracker.setTracks([1: windowBox(rect: home.insetBy(dx: -4, dy: -4))])
        _ = rig.tracker.tick()
        rig.tracker.scrollWheel(at: CGPoint(x: 400, y: 400))
        XCTAssertNil(rig.tracker.tick())  // still shown
        XCTAssertEqual(rig.elements.monitor, [])
        XCTAssertEqual(rig.elements.checks.count, 0)
    }

    func testSharedScrollBarKeptWhileASiblingBandLives() {  // item 10
        let rig = makeRig()
        rig.elements.held = [5]
        rig.tracker.setTracks([1: band(scrollBar: 5), 2: band(scrollBar: 5, lines: 30...31)])
        rig.tracker.setTracks([2: band(scrollBar: 5, lines: 30...31)])
        XCTAssertEqual(rig.elements.held, [5])
        rig.tracker.setTracks([:])
        XCTAssertEqual(rig.elements.held, [])
    }

    func testReplacedAnchorStartsFresh() {  // item 5: a late re-resolve upgrades a drawn box
        let rig = makeRig()
        rig.tracker.setTracks([1: windowBox()])
        _ = rig.tracker.tick()
        rig.tracker.setTracks([1: element()])  // the model replaced the anchor
        XCTAssertNil(rig.tracker.tick())  // shown at the new box
        rig.windows.windows[1]?.frame.origin.x += 10
        XCTAssertEqual(rig.tracker.tick()?.rects[1], elementBox.offsetBy(dx: 10, dy: 0))
    }

    // MARK: round 2

    func testRefindStaysInTheWindowOfTheOriginalRect() {  // item 1
        // Two overlapping windows of two apps show the same label; the shape was grounded in pid 7's.
        let front = CGRect(x: 600, y: 100, width: 800, height: 600)
        let windows: [ShapeTrack.Window] = [(2, 9, front), (1, 7, home)]
        let stale = CGRect(x: 300, y: 300, width: 48, height: 24)
        let other = (text: "Inbox", rect: CGRect(x: 650, y: 302, width: 40, height: 16))  // nearer, pid 9's window
        let own = (text: "Inbox", rect: CGRect(x: 120, y: 650, width: 40, height: 16))
        XCTAssertEqual(ShapeTrack.owner(of: stale, windows: windows), 1)
        XCTAssertEqual(ShapeTrack.owner(of: other.rect, windows: windows), 2)  // the overlap belongs to the front window
        XCTAssertFalse(ShapeTrack.sameWindow(other.rect, as: stale, windows: windows))
        XCTAssertTrue(ShapeTrack.sameWindow(own.rect, as: stale, windows: windows))
        XCTAssertEqual(ShapeTrack.refind(label: "Inbox", stale: stale, observations: [other, own],
                                         windows: windows, grounded: true),
                       own.rect.insetBy(dx: -4, dy: -4))
        // Only the other window shows it: rejected, the shape falls back to its original rect.
        XCTAssertNil(ShapeTrack.refind(label: "Inbox", stale: stale, observations: [other], windows: windows, grounded: true))
        // A rect the brain wrote has no window of its own: the nearest match wins.
        XCTAssertEqual(ShapeTrack.refind(label: "Inbox", stale: stale, observations: [other, own],
                                         windows: windows, grounded: false),
                       other.rect.insetBy(dx: -4, dy: -4))
    }

    func testModelRectIsDrawnOnlyWhereItsLabelIsSeen() {  // item 1: live check (d)
        // Wikipedia in front, the debug window behind it on the left (same app). The brain wrote
        // (150,170,270,45) for "Saudi Arabia": over the debug window, where no capture item was.
        let wiki = CGRect(x: 443, y: 30, width: 572, height: 795)
        let debug = CGRect(x: 0, y: 30, width: 1100, height: 800)
        let windows: [ShapeTrack.Window] = [(2, 9, wiki), (1, 9, debug)]
        let brain = CGRect(x: 150, y: 170, width: 270, height: 45)
        let heading = CGRect(x: 506, y: 178, width: 163, height: 32)
        XCTAssertFalse(ShapeTrack.grounded(brain, in: [heading, wiki, debug]))
        XCTAssertTrue(ShapeTrack.grounded(heading.insetBy(dx: -4, dy: -4), in: [heading]))  // a ground.py OCR pick
        XCTAssertTrue(ShapeTrack.grounded(wiki.insetBy(dx: -4, dy: -4), in: [heading, wiki]))  // a window pick
        XCTAssertEqual(ShapeTrack.refind(label: "Saudi Arabia", stale: brain, observations: [("Saudi Arabia", heading)],
                                         windows: windows, grounded: false),
                       heading.insetBy(dx: -4, dy: -4))
        // Label not re-found: nothing is drawn. Before, the rect was anchored to the debug window.
        XCTAssertEqual(ShapeTrack.placement(fresh: nil, stale: brain, windows: windows, screens: [screen], grounded: false),
                       .gone)
        guard case .anchored(let old) = ShapeTrack.placement(fresh: nil, stale: brain, windows: windows, screens: [screen])
        else { return XCTFail("a grounded rect still falls back to its original spot") }
        XCTAssertEqual(old.windowID, 1)
    }

    func testPositionLookupNeedsTheLabel() {  // item 1: an element found only by position
        let box = CGRect(x: 496, y: 44, width: 92, height: 22)
        let frame = CGRect(x: 490, y: 40, width: 110, height: 30)
        XCTAssertNil(ShapeTrack.acceptElement(box: box, elementFrame: frame, role: "AXStaticText", value: "Drafts",
                                              label: "", shapeLabel: "Inbox", mustName: true))
        XCTAssertEqual(ShapeTrack.acceptElement(box: box, elementFrame: frame, role: "AXStaticText", value: "Inbox (3)",
                                                label: "", shapeLabel: "Inbox", mustName: true)?.fit, .offset)
        XCTAssertEqual(ShapeTrack.acceptElement(box: box, elementFrame: frame, role: "AXStaticText", value: "Drafts",
                                                label: "", shapeLabel: "Inbox")?.fit, .offset)  // re-found by text
    }

    /// One Terminal-like text line as the content check sees it: Menlo 11 pt drawn at 2x into a
    /// 145x22 pt box, reduced to the 64x64 gray bitmap the app draws (ElementTrack.lumaGrid(of:))
    /// and then to its 16x16 grid. `ink`/`paper`: gray 0...1.
    private func textLine(_ text: String, ink: CGFloat, paper: CGFloat, cursor: Bool = false) -> [UInt8] {
        let space = CGColorSpaceCreateDeviceGray()
        guard let line = CGContext(data: nil, width: 290, height: 44, bitsPerComponent: 8, bytesPerRow: 0,
                                   space: space, bitmapInfo: CGImageAlphaInfo.none.rawValue)
        else { return [] }
        line.setFillColor(gray: paper, alpha: 1)
        line.fill(CGRect(x: 0, y: 0, width: 290, height: 44))
        let attributes: [NSAttributedString.Key: Any] = [
            NSAttributedString.Key(kCTFontAttributeName as String): CTFontCreateWithName("Menlo" as CFString, 22, nil),
            NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(gray: ink, alpha: 1),
        ]
        let typeset = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attributes) as CFAttributedString)
        line.textPosition = CGPoint(x: 8, y: 14)
        CTLineDraw(typeset, line)
        if cursor {
            line.setFillColor(gray: ink, alpha: 1)
            line.fill(CGRect(x: 8 + CTLineGetTypographicBounds(typeset, nil, nil, nil), y: 10, width: 13, height: 26))
        }
        guard let image = line.makeImage() else { return [] }
        var pixels = [UInt8](repeating: 0, count: 64 * 64)
        let drawn = pixels.withUnsafeMutableBytes { buffer -> Bool in
            guard let small = CGContext(data: buffer.baseAddress, width: 64, height: 64, bitsPerComponent: 8,
                                        bytesPerRow: 64, space: space, bitmapInfo: CGImageAlphaInfo.none.rawValue)
            else { return false }
            small.interpolationQuality = .medium
            small.draw(image, in: CGRect(x: 0, y: 0, width: 64, height: 64))
            return true
        }
        return drawn ? ShapeTrack.lumaGrid(luma: pixels, width: 64, height: 64) : []
    }

    func testContentCheckSeesRealTextLinesChange() {  // item 2a: thin text, both polarities
        for (ink, paper) in [(CGFloat(0.85), CGFloat(0.12)), (CGFloat(0.1), CGFloat(1)), (CGFloat(0.55), CGFloat(0.16))] {
            let zebra = textLine("zebra-quartz-17", ink: ink, paper: paper)
            XCTAssertEqual(zebra.count, 256)
            XCTAssertFalse(ShapeTrack.contentMoved(reference: zebra, current: textLine("zebra-quartz-17", ink: ink, paper: paper)))
            let tinted = textLine("zebra-quartz-17", ink: ink * 0.95, paper: paper * 0.95)  // dimmed or hover-tinted
            XCTAssertFalse(ShapeTrack.contentMoved(reference: zebra, current: tinted), "tint \(paper)")
            for other in ["user@dev-mac", "bash-3.2$ tail -f", "[1] 75968", ""] {
                XCTAssertTrue(ShapeTrack.contentMoved(reference: zebra, current: textLine(other, ink: ink, paper: paper)),
                              "\(other) on \(paper)")
            }
            XCTAssertTrue(ShapeTrack.contentMoved(reference: zebra,
                                                  current: textLine("bash-3.2$", ink: ink, paper: paper, cursor: true)))
        }
    }

    private func terminalLine(_ text: String) -> [UInt8] { textLine(text, ink: 0.85, paper: 0.12) }

    func testScrollBarBoxIsContentCheckedAndRemovedWhenTextChanges() {  // item 2a, 3: Terminal output
        let rig = makeRig()
        rig.elements.held = [5]
        rig.tracker.setTracks([1: windowBox(scrollBar: 5)])
        _ = rig.tracker.tick()
        XCTAssertEqual(rig.elements.checks, [[1: windowRect]])  // reference at placement, not 1 s later
        rig.tracker.receiveContent([1: terminalLine("zebra-quartz-17")], rects: [1: windowRect])
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 1)])
        rig.setClock(1)
        XCTAssertNil(rig.tracker.tick())
        rig.tracker.receive(pid: 7, reads: [5: ElementRead(status: .ok, scrollValue: 1)])  // still at the end
        XCTAssertEqual(rig.elements.checks.count, 2)
        rig.tracker.receiveContent([1: terminalLine("user@dev-mac")], rects: [1: windowRect])
        XCTAssertEqual(rig.tracker.tick(), FollowUpdate(removed: [1]))
        // The last box gone: no timer, no scroll monitor, no AX handle.
        XCTAssertFalse(rig.tracker.isRunning)
        XCTAssertEqual(rig.scheduler.stops, 1)
        XCTAssertEqual(rig.elements.monitor, [true, false])
        XCTAssertEqual(rig.elements.held, [])
        rig.setClock(5)
        XCTAssertNil(rig.tracker.tick())
        XCTAssertEqual(rig.elements.checks.count, 2)
    }

    func testSizeOnlyChangeIsPublished() {  // item 5
        let rig = makeRig()
        rig.tracker.setTracks([1: element()])
        rig.tracker.receive(pid: 7, reads: [1: read(CGRect(x: 300, y: 400, width: 460, height: 30))])
        XCTAssertEqual(rig.tracker.tick()?.rects[1], CGRect(x: 296, y: 396, width: 468, height: 38))
    }

    func testOnlyContentEventsExtendTheIdleClear() {  // item 2b
        XCTAssertTrue(ShapeTrack.extendsIdle(.say(text: "Here.")))
        XCTAssertTrue(ShapeTrack.extendsIdle(.audio(text: "Here.", rate: 24000, pcm: Data())))
        XCTAssertTrue(ShapeTrack.extendsIdle(.shape(HUDShape(kind: .rect, box: [0, 0, 10, 10]), check: nil)))
        XCTAssertTrue(ShapeTrack.extendsIdle(.lines(file: "a.swift", ranges: [1...2])))
        XCTAssertFalse(ShapeTrack.extendsIdle(.clear))
        XCTAssertFalse(ShapeTrack.extendsIdle(.done(metrics: [:], error: nil)))
        XCTAssertFalse(ShapeTrack.extendsIdle(.unknown))
    }

    // MARK: round 3

    func testWindowSizedBoxIsNeverDrawnForAThingInTheWindow() {  // item 1: live check (d) replay
        // Wikipedia on the right, the debug page on the left. "Highlight the heading" came back as
        // the whole Wikipedia window padded 4 pt, label "Safari: Saudi Arabia - Wikipedia".
        let wiki = CGRect(x: 855, y: 28, width: 573, height: 796)
        let debug = CGRect(x: 6, y: 28, width: 574, height: 800)
        let windows: [ShapeTrack.Window] = [(2, 9, wiki), (1, 9, debug)]
        let windowBox = wiki.insetBy(dx: -4, dy: -4)
        XCTAssertTrue(ShapeTrack.windowSized(windowBox, window: wiki))
        XCTAssertEqual(ShapeTrack.placement(fresh: nil, stale: windowBox, windows: windows, screens: [screen]), .gone)
        XCTAssertEqual(ShapeTrack.placement(fresh: windowBox, stale: windowBox, windows: windows, screens: [screen]), .gone)
        // A container-sized candidate (75 % of the window) is rejected; half the window is not.
        let container = CGRect(x: 855, y: 100, width: 573, height: 600)
        XCTAssertEqual(ShapeTrack.placement(fresh: container, stale: container, windows: windows, screens: [screen]), .gone)
        let half = CGRect(x: 855, y: 100, width: 573, height: 398)
        XCTAssertFalse(ShapeTrack.windowSized(half, window: wiki))
        guard case .anchored = ShapeTrack.placement(fresh: half, stale: half, windows: windows, screens: [screen])
        else { return XCTFail("half the window is still a thing in it") }
        // The real h1 is drawn, in its own window.
        let heading = CGRect(x: 915, y: 189, width: 171, height: 28)
        guard case .anchored(let anchor) = ShapeTrack.placement(fresh: heading, stale: windowBox, windows: windows,
                                                                screens: [screen])
        else { return XCTFail("the heading must be drawn") }
        XCTAssertEqual(anchor.windowID, 2)
        XCTAssertEqual(anchor.rect, heading)
    }

    func testExplicitWindowPickStillDrawsTheWindow() {  // item 1: "highlight the Safari window"
        let wiki = CGRect(x: 855, y: 28, width: 573, height: 796)
        let windows: [ShapeTrack.Window] = [(2, 9, wiki)]
        let windowBox = wiki.insetBy(dx: -4, dy: -4)
        XCTAssertEqual(ShapeTrack.matchWindow(stale: windowBox, windows: windows), windowBox)
        XCTAssertEqual(ShapeTrack.placement(fresh: windowBox, stale: windowBox, windows: windows, screens: [screen],
                                            windowPick: true),
                       .anchored(ShapeAnchor(windowID: 2, pid: 9, windowFrame: wiki, rect: windowBox)))
    }

    func testTallFieldIsNotSnapped() {  // a word in a multi-line field is not the whole field
        let word = CGRect(x: 300, y: 200, width: 60, height: 22)
        XCTAssertNil(ShapeTrack.acceptElement(box: word, elementFrame: home, role: "AXTextField",
                                              value: "a long text with the word inside", label: "", shapeLabel: "word"))
        XCTAssertNil(ShapeTrack.acceptElement(box: word, elementFrame: home, role: "AXTextArea",
                                              value: "a long text with the word inside", label: "", shapeLabel: "word"))
        let line = CGRect(x: 290, y: 195, width: 300, height: 30)
        XCTAssertEqual(ShapeTrack.acceptElement(box: word, elementFrame: line, role: "AXTextField",
                                                value: "the word", label: "", shapeLabel: "word")?.fit, .snap)
    }

    func testSnappedFieldTakesItsWrapperFrame() {  // item 2: Safari address field, fake AX tree
        // The AX text field is text-wide; the focus ring is drawn by its parent; the toolbar is above.
        let field = CGRect(x: 281, y: 47, width: 172, height: 33)
        let box = field.insetBy(dx: -4, dy: -4)
        let ring = CGRect(x: 256, y: 37, width: 248, height: 53)
        let toolbar = CGRect(x: 6, y: 28, width: 574, height: 60)
        XCTAssertEqual(ShapeTrack.acceptElement(box: box, elementFrame: field, role: "AXTextField", value: "127.0.0.1",
                                                label: "smart search field", shapeLabel: "smart search field")?.fit, .snap)
        XCTAssertEqual(ShapeTrack.fieldWrapper(box: box, field: field, ancestors: [ring, toolbar]), 0)
        let inner = CGRect(x: 279, y: 45, width: 176, height: 37)
        XCTAssertEqual(ShapeTrack.fieldWrapper(box: box, field: field, ancestors: [inner, ring, toolbar]), 1)
        XCTAssertNil(ShapeTrack.fieldWrapper(box: box, field: field, ancestors: [toolbar]))  // too wide
        XCTAssertNil(ShapeTrack.fieldWrapper(box: box, field: field, ancestors: [CGRect(x: 600, y: 37, width: 200, height: 53)]))
        XCTAssertNil(ShapeTrack.fieldWrapper(box: box, field: field, ancestors: []))
    }

    func testLabelIsNeverInTheMenuBar() {  // item 1: the window box label sat in the menu bar
        XCTAssertEqual(ShapeTrack.labelCentreY(for: CGRect(x: 851, y: 24, width: 581, height: 804)), 48)  // inside, top
        XCTAssertEqual(ShapeTrack.labelCentreY(for: CGRect(x: 915, y: 189, width: 171, height: 28)), 175)  // above
        XCTAssertEqual(ShapeTrack.labelCentreY(for: CGRect(x: 100, y: 30, width: 80, height: 24)), 68)  // below
        XCTAssertEqual(ShapeTrack.labelCentreY(for: CGRect(x: 10, y: -4, width: 60, height: 32)), 48)  // menu item
        // Safari's address-field wrapper (y33-94, 61 pt): below the box, not on the URL text.
        XCTAssertEqual(ShapeTrack.labelCentreY(for: CGRect(x: 252, y: 33, width: 256, height: 61)), 108)
    }
}
