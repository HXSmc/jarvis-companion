import AppKit
import SwiftUI
import OverlayCore

/// One card in its own panel: scales in from 0.96 on arrival (none with Reduce Motion; the
/// panel itself fades in). Hovering pauses the idle clear.
@MainActor
struct CardTile: View {
    let model: OverlayModel
    let card: HUDCard
    @State private var entered = false

    var body: some View {
        CardView(card: card)
            .scaleEffect(entered ? 1 : 0.96)
            .onAppear {
                guard !Theme.reduceMotion else {
                    entered = true
                    return
                }
                withAnimation(.easeOut(duration: 0.2)) { entered = true }
            }
            .onHover { model.setHovering(card.listKey, $0) }
    }
}

/// Parked cards as chips at their strip offsets; a click on any chip restores every card.
@MainActor
struct ChipStrip: View {
    static let hoverID = "chips"

    struct Chip {
        let card: HUDCard
        let offset: CGPoint
    }

    let model: OverlayModel
    let chips: [Chip]

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(chips, id: \.card.listKey) { chip in
                Button { model.setLayout(.restore) } label: { CardChip(card: chip.card) }
                    .buttonStyle(.plain)
                    .offset(x: chip.offset.x, y: chip.offset.y)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .onHover { model.setHovering(Self.hoverID, $0) }
    }
}

/// The permission card in its own top-right panel; hovering pauses the idle clear.
@MainActor
struct PermissionTile: View {
    static let hoverID = "permission"

    let model: OverlayModel
    let missing: [Permission]

    var body: some View {
        PermissionCard(missing: missing)
            .onHover { model.setHovering(Self.hoverID, $0) }
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
