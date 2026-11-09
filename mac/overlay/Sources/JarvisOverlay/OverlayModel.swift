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

    /// Any content event (shape/card/say) restarts the 60 s idle countdown.
    func activity() { bumpIdle() }

    func add(shape: HUDShape) {
        shapes.append(shape)
        bumpIdle()
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
        shapes = []
        cards = []
        idleTask?.cancel()
        idleTask = nil
    }

    private func bumpIdle() {
        idleTask?.cancel()
        idleTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 60_000_000_000)
            guard !Task.isCancelled else { return }
            self?.shapes = []
            self?.cards = []
        }
    }
}
