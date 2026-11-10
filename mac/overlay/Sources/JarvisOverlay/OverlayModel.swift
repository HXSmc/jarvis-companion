import Foundation
import Combine
import OverlayCore

/// Single source of truth for everything the overlay draws.
@MainActor
final class OverlayModel: ObservableObject {
    @Published var shapes: [HUDShape] = []
    /// Newest last (bottom of the column), max 5; a card with the same id replaces the old one.
    @Published var cards: [HUDCard] = []
    /// Missing system permissions; drives the native permission card, which the
    /// idle timer and clear() must not remove.
    @Published var missingPermissions: [Permission] = []
    @Published var orb: OrbState = .idle
    /// 0...1 mic/output level for the orb.
    @Published var level: Double = 0
    /// One-line status strip (service down); nil when hidden.
    @Published var banner: String?

    private var idleTask: Task<Void, Never>?
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
        var anchor: ShapeAnchor?
        var hidden = false
    }

    /// Any content event (shape/card/say) restarts the 60 s idle countdown.
    func activity() { bumpIdle() }

    /// Returns the key that anchor(_:) takes once the shape's window is known.
    @discardableResult
    func add(shape: HUDShape) -> Int {
        nextShapeKey += 1
        entries.append(ShapeEntry(key: nextShapeKey, shape: shape))
        let dropsAnchor = entries.count > Self.maxShapes && entries[0].anchor != nil
        entries.dropOldest(keeping: Self.maxShapes)
        publishShapes()
        if dropsAnchor { syncFollow() }
        bumpIdle()
        return nextShapeKey
    }

    /// Ties shapes to the windows they were drawn on; keys already gone are ignored.
    func anchor(_ anchors: [Int: ShapeAnchor]) {
        guard !anchors.isEmpty else { return }
        for index in entries.indices {
            if let anchor = anchors[entries[index].key] { entries[index].anchor = anchor }
        }
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
        if let id = card.id, let index = cards.firstIndex(where: { $0.id == id }) {
            cards[index] = card
        } else {
            cards.append(card)
        }
        if cards.count > 5 { cards.removeFirst(cards.count - 5) }
        bumpIdle()
    }

    /// Oldest card (top of the column) goes first when the panel must shrink.
    func dropOldestCard() {
        guard !cards.isEmpty else { return }
        cards.removeFirst()
    }

    /// Pauses the 60 s idle clear while the pointer is over the card panel.
    func setIdlePaused(_ paused: Bool) {
        if paused {
            idleTask?.cancel()
            idleTask = nil
        } else {
            bumpIdle()
        }
    }

    func clear() {
        clearShapes()
        cards = []
        idleTask?.cancel()
        idleTask = nil
    }

    private func bumpIdle() {
        idleTask?.cancel()
        idleTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 60_000_000_000)
            guard !Task.isCancelled else { return }
            self?.clearShapes()
            self?.cards = []
        }
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
        var anchors: [Int: ShapeAnchor] = [:]
        for entry in entries { if let anchor = entry.anchor { anchors[entry.key] = anchor } }
        if follower == nil {
            guard !anchors.isEmpty else { return }
            follower = WindowFollower { [weak self] update in self?.apply(update) }
        }
        follower?.setAnchors(anchors)
    }
}
