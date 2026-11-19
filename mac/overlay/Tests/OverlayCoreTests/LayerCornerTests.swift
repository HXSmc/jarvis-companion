import XCTest
@testable import OverlayCore

/// f52-layer. A layered card prefers the far (trailing) corner of the card under it, so the
/// lower card's title strip and value stay readable. Top-left points, y down.
/// Laptop: 1440x900 screen, 25 pt menu bar. Ultrawide: 3440x1440 screen. Keep-outs as the app
/// passes them: orb, notification zone and the permission card.
private struct Screen {
    let screen: CGRect
    let visible: CGRect
    var area: CGRect { ScatterLayout.insetArea(visible) }
    var keepOut: [CGRect] {
        [ScatterLayout.orbKeepOut(screen: screen), ScatterLayout.notificationZone(visible: visible),
         CGRect(x: visible.maxX - 16 - 320, y: visible.minY + 16, width: 320, height: 120)]
    }
}

private let laptop = Screen(screen: CGRect(x: 0, y: 0, width: 1440, height: 900),
                            visible: CGRect(x: 0, y: 25, width: 1440, height: 875))
private let ultrawide = Screen(screen: CGRect(x: 0, y: 0, width: 3440, height: 1440),
                               visible: CGRect(x: 0, y: 25, width: 3440, height: 1415))
private let sizes = [CGSize(width: 320, height: 160), CGSize(width: 340, height: 360), CGSize(width: 260, height: 120),
                     CGSize(width: 300, height: 200), CGSize(width: 220, height: 90), CGSize(width: 320, height: 160)]

/// One placement to check: 6-12 existing cards, a new card whose key prefers layering.
private struct Case {
    let screen: Screen
    let existing: [CGRect]
    let size: CGSize
    let key: String
}

/// The same placements on every run: existing cards at seeded spots that keep `minGap` from each
/// other and from the keep-outs (a started session), 100 per screen.
private func cases(prefersLayer: Bool) -> [Case] {
    var result: [Case] = []
    for screen in [laptop, ultrawide] {
        for index in 0..<100 {
            var rng = ScatterLayout.SplitMix64(state: ScatterLayout.seed("f52-layer-\(prefersLayer)-\(screen.visible.width)-\(index)"))
            let count = 6 + index % 7
            var existing: [CGRect] = []
            var tries = 0
            while existing.count < count, tries < 2000 {
                tries += 1
                let size = sizes[(existing.count + index) % sizes.count]
                let frame = CGRect(x: screen.area.minX + rng.unit() * (screen.area.width - size.width),
                                   y: screen.area.minY + rng.unit() * (screen.area.height - size.height),
                                   width: size.width, height: size.height)
                if screen.keepOut.contains(where: { $0.intersects(frame) }) { continue }
                if existing.contains(where: { $0.insetBy(dx: -ScatterLayout.minGap, dy: -ScatterLayout.minGap).intersects(frame) }) { continue }
                existing.append(frame)
            }
            var key = "card-\(index)"
            var n = 0
            while ScatterLayout.prefersLayer(key: key, existingCount: existing.count) != prefersLayer, n < 500 {
                n += 1
                key = "card-\(index)-\(n)"
            }
            result.append(Case(screen: screen, existing: existing, size: sizes[(index + 3) % sizes.count], key: key))
        }
    }
    return result
}

private func place(_ test: Case, keepOut: [CGRect]? = nil) -> ScatterLayout.Placement {
    ScatterLayout.place(size: test.size, key: test.key, visible: test.screen.visible, existing: test.existing,
                        keepOut: keepOut ?? test.screen.keepOut)
}

/// Overlaps exactly one card, inside `layerBand`.
private func isLayered(_ frame: CGRect, over existing: [CGRect]) -> Bool {
    ScatterLayout.fitness(frame, existing: existing, covered: ScatterLayout.coverage(existing))?.layers ?? false
}

final class LayerCornerTests: XCTestCase {

    /// Layered placements the 200 layering cases gave before this change (`297c36b`), recorded from the
    /// old algorithm on the same inputs.
    private static let baseLayeredCount = 184
    /// Non-layering placements that found a spot before this change, and the sum of their minX + minY.
    private static let baseFlatPlaced = 188
    private static let baseFlatChecksum = 304433.9146

    /// Every candidate frame a layering `place` weighs: the corner origins of every card, then the
    /// seeded random spots, replayed from the key's seed exactly as `place` draws them. Only frames
    /// clear of the keep-outs and valid as layered.
    private func layeredCandidates(_ test: Case) -> [CGRect] {
        let area = test.screen.area
        let size = CGSize(width: min(test.size.width, area.width), height: min(test.size.height, area.height))
        var rng = ScatterLayout.SplitMix64(state: ScatterLayout.seed(test.key))
        _ = (0..<9).map { _ in rng.next() }
        let zoneSize = CGSize(width: area.width / 3, height: area.height / 3)
        var frames: [CGRect] = []
        for zone in 0..<9 {
            let zoneOrigin = CGPoint(x: area.minX + CGFloat(zone % 3) * zoneSize.width,
                                     y: area.minY + CGFloat(zone / 3) * zoneSize.height)
            var origins: [CGPoint] = []
            for _ in 0..<ScatterLayout.candidatesPerZone {
                origins.append(CGPoint(x: zoneOrigin.x + rng.unit() * zoneSize.width - size.width / 2,
                                       y: zoneOrigin.y + rng.unit() * zoneSize.height - size.height / 2))
            }
            for other in test.existing {
                origins += ScatterLayout.layerOrigins(size: size, over: other)
            }
            frames += origins.map { ScatterLayout.clamp(CGRect(origin: $0, size: size), into: area) }
        }
        return frames.filter { frame in
            !test.screen.keepOut.contains { $0.intersects(frame) } && isLayered(frame, over: test.existing)
        }
    }

    private func coversAnyTitle(_ frame: CGRect, _ existing: [CGRect]) -> Bool {
        existing.contains { ScatterLayout.coversTitle(frame, of: $0) }
    }

    // MARK: criteria 1 and 2: the far corner first, the title strip protected

    func testLayeredFrameAvoidsTitleStripUnlessNothingElseFits() {
        var layered = 0
        for test in cases(prefersLayer: true) {
            guard case .at(let frame) = place(test), isLayered(frame, over: test.existing) else { continue }
            layered += 1
            guard coversAnyTitle(frame, test.existing) else { continue }
            let clear = layeredCandidates(test).filter { !coversAnyTitle($0, test.existing) }
            XCTAssertTrue(clear.isEmpty, "layered on a title strip although \(clear.count) clear layered spots exist: \(test.key) \(frame)")
        }
        XCTAssertGreaterThan(layered, 100, "the sweep must layer most of its cards")
    }

    func testFarCornerIsTriedBeforeTheTitleCorner() {
        let size = CGSize(width: 240, height: 120)
        let other = CGRect(x: 600, y: 400, width: 320, height: 160)
        let origins = ScatterLayout.layerOrigins(size: size, over: other)
        XCTAssertEqual(origins.count, 4)
        // trailing-bottom, trailing-top, leading-bottom, leading-top (the title corner)
        XCTAssertGreaterThan(origins[0].x + size.width / 2, other.midX)
        XCTAssertGreaterThan(origins[0].y + size.height / 2, other.midY)
        XCTAssertGreaterThan(origins[1].x + size.width / 2, other.midX)
        XCTAssertLessThan(origins[1].y + size.height / 2, other.midY)
        XCTAssertLessThan(origins[2].x + size.width / 2, other.midX)
        XCTAssertGreaterThan(origins[2].y + size.height / 2, other.midY)
        XCTAssertLessThan(origins[3].x + size.width / 2, other.midX)
        XCTAssertLessThan(origins[3].y + size.height / 2, other.midY)
        let strips = origins.map { ScatterLayout.coversTitle(CGRect(origin: $0, size: size), of: other) }
        XCTAssertEqual(strips, [false, false, false, true])
    }

    func testTitleStripIsTheLeadingSixtyPercentFortyFourTall() {
        let card = CGRect(x: 100, y: 200, width: 300, height: 160)
        XCTAssertEqual(ScatterLayout.titleStrip(of: card), CGRect(x: 100, y: 200, width: 180, height: ScatterLayout.minStrip))
        // a shared edge is not a hit
        XCTAssertFalse(ScatterLayout.coversTitle(CGRect(x: 280, y: 200, width: 100, height: 100), of: card))
        XCTAssertFalse(ScatterLayout.coversTitle(CGRect(x: 100, y: 244, width: 100, height: 100), of: card))
        XCTAssertTrue(ScatterLayout.coversTitle(CGRect(x: 270, y: 230, width: 100, height: 100), of: card))
    }

    // MARK: criterion 3: the rules around it stay

    func testLayeredShareAndFlatPlacementsStayAsOnBase() {
        let layering = cases(prefersLayer: true)
        let layeredNow = layering.filter { test in
            if case .at(let frame) = place(test) { return isLayered(frame, over: test.existing) }
            return false
        }.count
        XCTAssertEqual(layeredNow, Self.baseLayeredCount, "same share of layered cards as before the change")

        let flat = cases(prefersLayer: false)
        var checksum = 0.0
        var placed = 0
        for test in flat {
            guard case .at(let frame) = place(test) else { continue }
            placed += 1
            checksum += Double(frame.minX + frame.minY)
        }
        XCTAssertEqual(placed, Self.baseFlatPlaced)
        XCTAssertEqual(checksum, Self.baseFlatChecksum, accuracy: 0.01, "non-layered placement is unchanged")
    }

    func testEveryOverlapStaysInBandAndUnderTheCap() {
        for test in cases(prefersLayer: true) + cases(prefersLayer: false) {
            guard case .at(let frame) = place(test) else { continue }
            for other in test.existing {
                let overlap = ScatterLayout.overlapArea(frame, other)
                guard overlap > 0 else { continue }
                let ratio = overlap / min(ScatterLayout.area(frame), ScatterLayout.area(other))
                XCTAssertLessThanOrEqual(ratio, ScatterLayout.maxOverlap + 1e-9, "\(test.key)")
                XCTAssertGreaterThanOrEqual(ratio, ScatterLayout.minOverlap - 1e-9, "\(test.key)")
            }
            if isLayered(frame, over: test.existing) {
                let ratios = test.existing.map { ScatterLayout.overlapArea(frame, $0) / min(ScatterLayout.area(frame), ScatterLayout.area($0)) }
                XCTAssertTrue(ratios.contains { ScatterLayout.layerBand.contains($0) }, "layered frame out of band: \(test.key)")
            }
        }
    }

    func testKeepOutsAreRespected() {
        for test in cases(prefersLayer: true) + cases(prefersLayer: false) {
            guard case .at(let frame) = place(test) else { continue }
            XCTAssertFalse(test.screen.keepOut.contains { $0.intersects(frame) }, "frame on a keep-out: \(test.key) \(frame)")
            XCTAssertTrue(test.screen.area.insetBy(dx: -0.01, dy: -0.01).contains(frame), "frame outside the screen: \(test.key)")
        }
    }

    func testSameInputsGiveTheSameFrame() {
        for test in cases(prefersLayer: true).prefix(40) {
            XCTAssertEqual(place(test), place(test), test.key)
        }
    }

    // MARK: criterion 5(e): a frame on the title strip is the last resort, not forbidden

    /// The title corner itself (leading-top) never passes `fitness` (`isCornerOverlap` keeps the lower
    /// card's top-left point clear), so the last resort that can happen is a layered frame on the strip
    /// that is not on that point: here the leading-bottom corner of a 60 pt tall card, whose 28 pt
    /// overlap reaches into the 44 pt strip.
    func testOnlyAFrameOnTheTitleStripFitsStillLayersThere() {
        let under = CGRect(x: 600, y: 400, width: 300, height: 60)
        let existing = [under,
                        CGRect(x: 30, y: 60, width: 200, height: 100), CGRect(x: 30, y: 220, width: 200, height: 100),
                        CGRect(x: 30, y: 380, width: 200, height: 100), CGRect(x: 30, y: 540, width: 200, height: 100),
                        CGRect(x: 30, y: 700, width: 200, height: 100)]
        let size = CGSize(width: 240, height: 120)
        var key = "last-resort"
        var n = 0
        while !ScatterLayout.prefersLayer(key: key, existingCount: existing.count), n < 500 { n += 1; key = "last-resort-\(n)" }
        let leadingBottom = ScatterLayout.layerOrigins(size: size, over: under)[2]
        let hole = CGRect(origin: leadingBottom, size: size)
        XCTAssertTrue(ScatterLayout.coversTitle(hole, of: under), "the constructed spot sits on the strip")
        // four keep-outs round the spot, 1 pt clear of it (edge contact counts as touching on some platforms)
        let open = hole.insetBy(dx: -1, dy: -1)
        let keepOut = [CGRect(x: 0, y: 0, width: open.minX, height: 900),
                       CGRect(x: open.maxX, y: 0, width: 1440 - open.maxX, height: 900),
                       CGRect(x: open.minX, y: 0, width: open.width, height: open.minY),
                       CGRect(x: open.minX, y: open.maxY, width: open.width, height: 900 - open.maxY)]
        let visible = CGRect(x: 0, y: 25, width: 1440, height: 875)
        guard case .at(let frame) = ScatterLayout.place(size: size, key: key, visible: visible, existing: existing, keepOut: keepOut) else {
            return XCTFail("no placement")
        }
        XCTAssertEqual(frame.minX, hole.minX, accuracy: 0.01)
        XCTAssertEqual(frame.minY, hole.minY, accuracy: 0.01)
        XCTAssertTrue(isLayered(frame, over: existing))
        // with the whole screen open the same card keeps off every title strip
        guard case .at(let free) = ScatterLayout.place(size: size, key: key, visible: visible, existing: existing, keepOut: []) else {
            return XCTFail("no open placement")
        }
        XCTAssertFalse(existing.contains { ScatterLayout.coversTitle(free, of: $0) }, "open screen: \(free)")
    }
}
