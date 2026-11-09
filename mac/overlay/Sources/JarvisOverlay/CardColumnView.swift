import AppKit
import SwiftUI
import OverlayCore

/// Card stack hosted in the CardPanel: permission card first, then HUD cards
/// with the newest at the bottom. Hovering pauses the idle clear.
@MainActor
struct CardColumnView: View {
    @ObservedObject var model: OverlayModel

    var body: some View {
        VStack(spacing: 12) {
            if !model.missingPermissions.isEmpty {
                PermissionCard(missing: model.missingPermissions)
            }
            // Stable identity per card, so an insert or drop never re-binds a view to another card.
            ForEach(model.cards, id: \.listKey) { card in
                CardView(card: card)
                    .transition(.opacity)
            }
        }
        .onHover { model.setIdlePaused($0) }
        // 200 ms ease-out on inserts; nothing when the user set Reduce Motion.
        .animation(Theme.reduceMotion ? nil : .easeOut(duration: 0.2), value: model.cards)
    }
}

/// Native permission card (x-apple.systempreferences is not https, so this one
/// is not a HUD card): one row per missing pane, each opening its settings pane.
@MainActor
private struct PermissionCard: View {
    let missing: [Permission]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Permission needed")
                .font(Theme.cardTitle)
                .foregroundStyle(.white)
            ForEach(missing, id: \.self) { pane in
                Button {
                    NSWorkspace.shared.open(Permissions.settingsURL(pane))
                } label: {
                    Text("Open \(Permissions.paneName(pane)) settings")
                        .font(.system(size: 13))
                        .foregroundStyle(Theme.accent)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(14)
        .frame(width: 300, alignment: .leading)
        .background(Theme.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 4))
        .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.cardBorder, lineWidth: 1))
    }
}

extension HUDCard {
    /// The card's own id (the service always sets one); type + title when a card has none.
    var listKey: String { id ?? "\(typeName):\(title)" }

    var title: String {
        switch self {
        case .info(let p): return p.title
        case .stat(let p): return p.title
        case .bar(let p): return p.title
        case .flow(let p): return p.title
        case .link(let p): return p.title
        case .image(let p): return p.title
        }
    }
}
