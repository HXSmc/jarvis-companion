import Foundation
import Combine
import OverlayCore

/// Single source of truth for everything the overlay draws.
@MainActor
final class OverlayModel: ObservableObject {
    @Published var shapes: [HUDShape] = []
    /// Newest last, at most ScatterLayout.maxCards; a card with the same id replaces the old one.
    @Published var cards: [HUDCard] = [] {
        didSet {
            layout.keep(Set(cards.map(\.listKey)))
            MediaPlaybackController.shared.cardsChanged(old: oldValue, new: cards)
        }
    }
    /// Stored scatter frame per card and the normal / aside mode (CardPanel draws it).
    @Published private(set) var layout = ScatterLayout.LayoutState()
    /// Missing system permissions; drives the native permission card, which the
    /// idle timer and clear() must not remove.
    @Published var missingPermissions: [Permission] = []
    @Published var orb: OrbState = .idle
    /// 0...1 mic/output level for the orb.
    @Published var level: Double = 0
    /// One-line status strip (service down); nil when hidden.
    @Published var banner: String?

    private var idleTask: Task<Void, Never>?
    /// Set by CardPanel: whether the pointer rests on a shown card, chip or the permission card
    /// right now. Read at the idle deadline only, so no remembered hover state can stick.
    var pointerOnPanel: @MainActor () -> Bool = { false }
    /// Set by CardPanel: the visible frame (top-left points) and the orb keep-out a group move uses.
    var moveBounds: @MainActor () -> (visible: CGRect, keepOut: [CGRect]) = { (.zero, []) }
    /// Set by CardPanel: a refused voice move leans the shown cards toward the asked side and back.
    var moveRefused: @MainActor (LayoutMode) -> Void = { _ in }
    private static let maxShapes = 40

    /// Every shape held, oldest first; `shapes` is the visible part (hidden anchored shapes
    /// are left out). Cap, idle clear and clear() act here.
    private var entries: [ShapeEntry] = []
    private var nextShapeKey = 0
    /// Created with the first anchored shape; its timer runs only while anchors exist.
    private var follower: WindowFollower?

    private struct ShapeEntry {
        let key: Int
        var shape: HUDShape
        var anchor: TrackAnchor?
        var hidden = false
    }

    /// Any content event (ShapeTrack.extendsIdle) restarts the 60 s idle countdown; follow
    /// updates, late re-resolves and commits never do.
    func activity() { bumpIdle() }

    /// Returns the key that anchor(_:) takes once the shape's window is known.
    @discardableResult
    func add(shape: HUDShape) -> Int {
        unhide()
        nextShapeKey += 1
        entries.append(ShapeEntry(key: nextShapeKey, shape: shape))
        let dropsAnchor = entries.count > Self.maxShapes && entries[0].anchor != nil
        entries.dropOldest(keeping: Self.maxShapes)
        publishShapes()
        if dropsAnchor { syncFollow() }
        // No idle bump: the shape's event already restarted it on arrival (a commit may come later).
        return nextShapeKey
    }

    /// Ties shapes to what they follow; returns the keys already gone (their anchors unused).
    @discardableResult
    func anchor(_ anchors: [Int: TrackAnchor]) -> [Int] {
        guard !anchors.isEmpty else { return [] }
        var unused = anchors
        for index in entries.indices {
            if unused.removeValue(forKey: entries[index].key) != nil { entries[index].anchor = anchors[entries[index].key] }
        }
        syncFollow()
        return Array(unused.keys)
    }

    /// A late re-resolve moves a drawn shape and replaces its anchor; false when it is gone or
    /// hidden (scrolled, covered: a result read before that must not show it again).
    func replace(key: Int, rect: CGRect, anchor: TrackAnchor?) -> Bool {
        guard let index = entries.firstIndex(where: { $0.key == key }), !entries[index].hidden else { return false }
        entries[index].shape.box = [Double(rect.minX), Double(rect.minY), Double(rect.maxX), Double(rect.maxY)]
        entries[index].anchor = anchor
        publishShapes()
        syncFollow()
        return true
    }

    /// A late re-resolve found the target positively gone.
    func remove(keys: [Int]) {
        entries.removeAll { keys.contains($0.key) }
        publishShapes()
        syncFollow()
    }

    /// Applies one follow tick (window moved, hidden, back or closed). Does not touch the
    /// idle countdown: only content events keep the overlay alive.
    func apply(_ update: FollowUpdate) {
        if !update.removed.isEmpty { entries.removeAll { update.removed.contains($0.key) } }
        for index in entries.indices where entries[index].anchor != nil {
            let key = entries[index].key
            if let rect = update.rects[key] {
                entries[index].hidden = false
                entries[index].shape.box = [Double(rect.minX), Double(rect.minY), Double(rect.maxX), Double(rect.maxY)]
            } else if update.hidden.contains(key) {
                entries[index].hidden = true
            }
        }
        publishShapes()
    }

    func add(card: HUDCard) {
        unhide()  // a new answer is never invisible
        // A Confirm question (or its settled line) is never left as a parked chip.
        if ScatterLayout.restoresParked(card) { setLayout(.restore) }
        if let id = card.id, let index = cards.firstIndex(where: { $0.id == id }) {
            cards[index] = card
        } else {
            cards.append(card)
        }
        // Over the cap the first card of the layout's retire order goes (pinned cards last).
        while cards.count > ScatterLayout.maxCards {
            let index = layout.retireOrder.compactMap { key in cards.firstIndex { $0.listKey == key } }.first ?? 0
            cards.remove(at: index)
        }
        bumpIdle()
    }

    /// Stores a measured card's scatter frame; cards retired to make room leave `cards`.
    func place(key: String, size: CGSize, visible: CGRect, keepOut: [CGRect]) {
        let retired = layout.cardAdded(key, size: size, visible: visible, keepOut: keepOut)
        if !retired.isEmpty { cards.removeAll { retired.contains($0.listKey) } }
    }

    /// A card replaced in place keeps its spot while its new size keeps the caps; otherwise it
    /// alone is placed again, and cards retired to make room leave `cards`.
    func resize(key: String, size: CGSize, visible: CGRect, keepOut: [CGRect]) {
        let retired = layout.cardResized(key, size: size, visible: visible, keepOut: keepOut)
        if !retired.isEmpty { cards.removeAll { retired.contains($0.listKey) } }
    }

    /// New line bands: cards over them move off once (ScatterLayout.LayoutState.moveOff).
    func moveCards(off highlights: [CGRect], visible: CGRect, keepOut: [CGRect]) {
        let retired = layout.moveOff(highlights, visible: visible, keepOut: keepOut)
        if !retired.isEmpty { cards.removeAll { retired.contains($0.listKey) } }
    }

    /// The user drags a card: it comes to the front and stays where it is dropped. The idle
    /// countdown restarts when the drag ends (during it the pointer rests on the card).
    func drag(key: String, to frame: CGRect, visible: CGRect, ended: Bool) {
        layout.cardDragged(key, to: frame, visible: visible)
        if ended { bumpIdle() }
    }

    /// The screen changed: every stored frame moves the least distance back on screen.
    func reclamp(visible: CGRect) {
        layout.reclamp(visible: visible)
    }

    /// Aside parks every card as a chip and drops the current highlight boxes; restore puts
    /// each card back at its stored frame and restarts the idle countdown. Hide fades every card
    /// and chip out (kept, idle countdown paused, highlights and media stopped); show and every
    /// other mode make them visible again. A move shows and restores the cards, then moves the
    /// group; a refused move (no room clear of the orb) changes nothing and nudges the cards.
    /// Repeats are no-ops.
    func setLayout(_ mode: LayoutMode) {
        switch mode {
        case .hide:
            guard !layout.hidden else { return }
            layout.hide()
            MediaPlaybackController.shared.stopAll()
            clearShapes()
            idleTask?.cancel()
            idleTask = nil
            return
        case .left, .right, .up, .down, .topLeft, .topRight, .bottomLeft, .bottomRight, .otherSide:
            let bounds = moveBounds()
            if layout.moveAll(mode, visible: bounds.visible, keepOut: bounds.keepOut) {
                bumpIdle()
            } else {
                moveRefused(mode)
            }
            return
        case .show, .aside, .restore:
            break
        }
        unhide()
        switch mode {
        case .hide, .show, .left, .right, .up, .down, .topLeft, .topRight, .bottomLeft, .bottomRight, .otherSide:
            break
        case .aside:
            guard layout.mode == .normal else { return }
            layout.aside()
            MediaPlaybackController.shared.stopAll()
            clearShapes()
        case .restore:
            guard layout.mode == .aside else { return }
            layout.restore()
            bumpIdle()
        }
    }

    /// Hidden cards come back (show, any other layout command, a new card or shape).
    private func unhide() {
        guard layout.hidden else { return }
        layout.show()
        bumpIdle()
    }

    func clear() {
        clearShapes()
        cards = []
        layout.clear()
        idleTask?.cancel()
        idleTask = nil
    }

    private func bumpIdle() {
        idleTask?.cancel()
        idleTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 60_000_000_000)
            guard !Task.isCancelled else { return }
            self?.idleExpired()
        }
    }

    /// At the deadline: the pointer resting on a panel (read now), or a video plays — held at
    /// most 10 min from its start — waits another round; otherwise clears the highlight boxes,
    /// and the cards unless they are parked aside (chips wait for restore or clear, no timer).
    private func idleExpired() {
        if ScatterLayout.idleWaits(pointerOnPanel: pointerOnPanel(),
                                   playingSince: MediaPlaybackController.shared.playingSince,
                                   now: Date()) {
            bumpIdle()
            return
        }
        clearShapes()
        if layout.mode == .normal && !layout.hidden { cards = [] }
    }

    private func clearShapes() {
        entries = []
        shapes = []
        syncFollow()
    }

    /// Republishes only when the visible list changed (a tick that moved nothing is free).
    private func publishShapes() {
        let visible = entries.filter { !$0.hidden }.map(\.shape)
        if visible != shapes { shapes = visible }
    }

    /// Hands the current anchors to the follower; no anchors stops its timer.
    private func syncFollow() {
        var anchors: [Int: TrackAnchor] = [:]
        for entry in entries { if let anchor = entry.anchor { anchors[entry.key] = anchor } }
        if follower == nil {
            guard !anchors.isEmpty else { return }
            follower = WindowFollower { [weak self] update in self?.apply(update) }
        }
        follower?.setAnchors(anchors)
    }
}
