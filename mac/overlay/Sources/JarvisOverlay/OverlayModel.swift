import Foundation
import Combine
import OverlayCore

/// Single source of truth for everything the overlay draws.
@MainActor
final class OverlayModel: ObservableObject {
    @Published var shapes: [HUDShape] = []
    /// Newest first, max 5; a card with the same id replaces the old one.
    @Published var cards: [HUDCard] = []
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
            cards.insert(card, at: 0)
        }
        if cards.count > 5 { cards.removeLast(cards.count - 5) }
        bumpIdle()
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
