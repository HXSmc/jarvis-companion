import SwiftUI
import OverlayCore

/// Newest-first card stack hosted in the CardPanel.
@MainActor
struct CardColumnView: View {
    @ObservedObject var model: OverlayModel

    var body: some View {
        VStack(spacing: 12) {
            ForEach(Array(model.cards.enumerated()), id: \.offset) { _, card in
                CardView(card: card)
                    .transition(.opacity)
            }
        }
        // 200 ms ease-out on inserts; nothing when the user set Reduce Motion.
        .animation(Theme.reduceMotion ? nil : .easeOut(duration: 0.2), value: model.cards)
    }
}
