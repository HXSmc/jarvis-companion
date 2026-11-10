import AppKit
import SwiftUI
import OverlayCore

/// One card in its own panel: scales in from 0.96 on arrival (none with Reduce Motion; the
/// panel itself fades in). Image and video cards render live media; the rest stay static.
@MainActor
struct CardTile: View {
    let card: HUDCard
    /// Whether media may load right now (the card is placed, visible, not aside, not retired).
    let active: Bool
    /// Lets a just-arrived single image refit its panel.
    let remeasure: () -> Void
    @State private var entered = false

    var body: some View {
        content
            .scaleEffect(entered ? 1 : 0.96)
            .onAppear {
                guard !Theme.reduceMotion else {
                    entered = true
                    return
                }
                withAnimation(.easeOut(duration: 0.2)) { entered = true }
            }
    }

    @ViewBuilder private var content: some View {
        switch card {
        case .image, .video:
            LiveMediaCard(card: card, active: active, remeasure: remeasure)
        default:
            CardView(card: card)
        }
    }
}

/// Live media inside a card panel: images load only while the card is shown and playable;
/// a video click goes to the one shared MediaPlaybackController.
@MainActor
struct LiveMediaCard: View {
    let card: HUDCard
    let active: Bool
    let remeasure: () -> Void
    @ObservedObject private var playback = MediaPlaybackController.shared
    @State private var states: [String: MediaImageState] = [:]

    var body: some View {
        switch card {
        case .image(let p):
            ImageCardView(card: p, states: states)
                .task(id: LoadKey(active: active, urls: p.images.map(\.url))) {
                    await load(p.images.map(\.url), single: p.images.count == 1)
                }
        case .video(let p):
            let thumb = p.youtubeID.map { MediaPolicy.thumbnailURL(id: $0).absoluteString } ?? p.thumbnailURL
            VideoCardView(card: p,
                          thumbnail: thumb.map { states[$0] ?? .loading },
                          phase: playback.phase(for: card.listKey),
                          onPlay: { playback.play(key: card.listKey) }) {
                player(p)
            }
            .task(id: LoadKey(active: active, urls: thumb.map { [$0] } ?? [])) {
                await load(thumb.map { [$0] } ?? [], single: false)
            }
        default:
            CardView(card: card)
        }
    }

    /// The inline player for a video card: the YouTube embed only. A direct video file shows its
    /// thumbnail and "Open in browser" (no inline player: its redirects and segments would
    /// bypass the address guard).
    @ViewBuilder private func player(_ p: VideoCard) -> some View {
        if let id = p.youtubeID {
            YouTubePlayerView(id: id, key: card.listKey)
        } else {
            EmptyView()
        }
    }

    /// Loads `urls` concurrently; a card that is aside, unplaced, queued or retired asks for
    /// nothing. Only terminal results are kept, so a re-run skips what already arrived.
    private func load(_ urls: [String], single: Bool) async {
        guard active else { return }
        let pending = urls.filter { states[$0] == nil }
        guard !pending.isEmpty else { return }
        // Children stay on the main actor, so no image crosses an actor boundary.
        await withTaskGroup(of: Void.self) { group in
            for url in pending {
                group.addTask { @MainActor in
                    let image = await MediaLoader.shared.image(for: url)
                    guard !Task.isCancelled else { return }
                    states[url] = image.map { MediaImageState.loaded($0) } ?? .failed
                    if single, image != nil { remeasure() }
                }
            }
        }
    }

    /// What a `.task` reloads on: the card's playability and the urls it shows.
    private struct LoadKey: Equatable {
        let active: Bool
        let urls: [String]
    }
}

/// One parked card as a chip in its own panel; a click on any chip restores every card.
@MainActor
struct CardChipButton: View {
    let model: OverlayModel
    let card: HUDCard

    var body: some View {
        Button { model.setLayout(.restore) } label: { CardChip(card: card) }
            .buttonStyle(.plain)
    }
}

/// Native permission card (x-apple.systempreferences is not https, so this one
/// is not a HUD card): one row per missing pane, each opening its settings pane.
@MainActor
struct PermissionCard: View {
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
