import SwiftUI
import AppKit
import Foundation

// MARK: - States

/// Load state of one image area; loaded images compare by identity.
public enum MediaImageState: Equatable {
    case loading
    case loaded(CGImage)
    case failed

    public static func == (lhs: MediaImageState, rhs: MediaImageState) -> Bool {
        switch (lhs, rhs) {
        case (.loading, .loading), (.failed, .failed):
            return true
        case (.loaded(let a), .loaded(let b)):
            return a === b
        default:
            return false
        }
    }
}

/// Lifecycle of the inline video area: starting = clicked, no frame yet.
public enum VideoPhase: Equatable {
    case idle
    case starting
    case playing
    case failed
}

// MARK: - Card chrome

/// Shared frame for media cards: fixed title bar, edge-to-edge media area, optional footer row,
/// on the same surface as CardChrome (material + 0.86 black, 1pt border, corner ticks).
@MainActor
private struct MediaChrome<Media: View, Footer: View>: View {
    let tag: String
    let title: String
    let hasFooter: Bool
    @ViewBuilder let media: () -> Media
    @ViewBuilder let footer: () -> Footer

    var body: some View {
        VStack(spacing: 0) {
            titleBar
            media()
                .clipped()
            if hasFooter {
                footer()
                    .frame(width: MediaPolicy.cardWidth, height: MediaPolicy.footerHeight)
            }
        }
        .frame(width: MediaPolicy.cardWidth, alignment: .leading)
        .background(
            ZStack {
                Rectangle().fill(.ultraThinMaterial)
                Rectangle().fill(Theme.cardBackground)
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard).strokeBorder(Theme.cardBorder, lineWidth: 1))
        .overlay(alignment: .topLeading) { CornerTicks().padding(6) }
    }

    private var titleBar: some View {
        HStack(spacing: 8) {
            Text(title)
                .font(Theme.cardTitle)
                .foregroundStyle(.white)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 8)
            Text(tag)
                .font(Theme.fontMicro)
                .foregroundStyle(Theme.accent)
                .fixedSize()
        }
        .padding(.horizontal, 14)
        .frame(width: MediaPolicy.cardWidth, height: MediaPolicy.titleBarHeight, alignment: .leading)
        .accessibilityAddTraits(.isHeader)
    }
}

/// Media cards without a footer row (image cards).
extension MediaChrome where Footer == EmptyView {
    init(tag: String, title: String, @ViewBuilder media: @escaping () -> Media) {
        self.init(tag: tag, title: title, hasFooter: false, media: media, footer: { EmptyView() })
    }
}

/// Plain placeholder tile (Theme fill + small text); the caller sizes it.
@MainActor
private struct MediaTile: View {
    let text: String

    var body: some View {
        ZStack {
            Rectangle().fill(Color.white.opacity(0.06))
            Text(text)
                .font(Theme.fontSmall)
                .foregroundStyle(Color.white.opacity(0.72))
                .lineLimit(1)
                .padding(.horizontal, 6)
        }
    }
}

/// Loaded images announce alt (or the card title); loading/failed tiles read their own text.
private extension View {
    @ViewBuilder
    func mediaTileLabel(state: MediaImageState, fallback: String) -> some View {
        if case .loaded = state {
            accessibilityLabel(fallback)
        } else {
            self
        }
    }
}

// MARK: - One image area

/// One image area in three states; the parent sets the frame (MediaPolicy), never the image.
@MainActor
public struct MediaImageView: View {
    private let state: MediaImageState
    private let alt: String?
    private let sourcePage: String?

    public init(state: MediaImageState, alt: String? = nil, sourcePage: String? = nil) {
        self.state = state
        self.alt = alt
        self.sourcePage = sourcePage
    }

    public var body: some View {
        ZStack {
            switch state {
            case .loading:
                loadingTile
                    .transition(.opacity)
            case .loaded(let image):
                Image(decorative: image, scale: 1)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .transition(.opacity)
            case .failed:
                failedTile
                    .transition(.opacity)
            }
        }
        .animation(Theme.reduceMotion ? nil : .easeOut(duration: 0.2), value: isLoaded)
    }

    private var isLoaded: Bool {
        if case .loaded = state { return true }
        return false
    }

    private var loadingTile: some View {
        ZStack {
            Rectangle().fill(Color.white.opacity(0.06))
            HStack(spacing: 8) {
                if !Theme.reduceMotion {
                    ProgressView().controlSize(.small)
                }
                Text("Loading image")
                    .font(Theme.fontSmall)
                    .foregroundStyle(Color.white.opacity(0.72))
            }
            .padding(.horizontal, 6)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading image")
    }

    private var failedTile: some View {
        ZStack {
            Rectangle().fill(Color.white.opacity(0.06))
            VStack(spacing: 8) {
                Image(systemName: "photo")
                    .font(Theme.fontBody)
                    .foregroundStyle(Color.white.opacity(0.5))
                    .accessibilityHidden(true)
                Text(alt ?? "Image unavailable")
                    .font(Theme.fontSmall)
                    .foregroundStyle(Color.white.opacity(0.72))
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 6)
                openSource
            }
        }
    }

    /// "Open source" appears only for a source page that passes the https check.
    @ViewBuilder
    private var openSource: some View {
        if let sourcePage, isSafeHTTPSURL(sourcePage) {
            Button {
                // Only https, re-checked here (payload is untrusted).
                if isSafeHTTPSURL(sourcePage), let url = URL(string: sourcePage) {
                    NSWorkspace.shared.open(url)
                }
            } label: {
                HStack(spacing: 4) {
                    Text("Open source")
                        .font(Theme.fontSmall)
                        .foregroundStyle(.white)
                    Image(systemName: "arrow.up.right")
                        .font(Theme.fontSmall)
                        .foregroundStyle(Theme.accent)
                }
                .padding(.horizontal, 12)
                .frame(minHeight: 32)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open source page")
        }
    }
}

// MARK: - Image card

/// Whole image card (title bar + single image or gallery grid), sized by MediaPolicy.
@MainActor
public struct ImageCardView: View {
    private let card: ImageCard
    private let states: [String: MediaImageState]

    public init(card: ImageCard, states: [String: MediaImageState] = [:]) {
        self.card = card
        self.states = states
    }

    public var body: some View {
        MediaChrome(tag: tag, title: card.title, media: { mediaArea })
    }

    /// "n FRAMES" for a gallery (capped by policy), "IMAGE" otherwise.
    private var tag: String {
        let count = min(card.images.count, MediaPolicy.maxMediaItems)
        return count >= 2 ? "\(count) FRAMES" : "IMAGE"
    }

    @ViewBuilder
    private var mediaArea: some View {
        let images = card.images
        if images.count >= 2 {
            gallery(images)
        } else if let item = images.first {
            singleImage(item)
        } else {
            MediaTile(text: card.fileID?.isEmpty == false ? "Saved image" : "No image")
                .frame(width: MediaPolicy.videoAreaSize.width, height: MediaPolicy.videoAreaSize.height)
        }
    }

    /// The loaded area takes its aspect from the pixels; every other state keeps the 16:9 tile.
    private func singleImage(_ item: ImageItem) -> some View {
        let state = states[item.url] ?? .loading
        let size: CGSize
        if case .loaded(let image) = state {
            size = MediaPolicy.imageAreaSize(pixelWidth: image.width, pixelHeight: image.height)
        } else {
            size = MediaPolicy.videoAreaSize
        }
        return MediaImageView(state: state, alt: item.alt, sourcePage: item.sourcePage)
            .frame(width: size.width, height: size.height)
            .clipped()
            .mediaTileLabel(state: state, fallback: item.alt ?? card.title)
    }

    /// Full rows from MediaPolicy.galleryRows (no empty cell), one row height, policy gaps.
    private func gallery(_ images: [ImageItem]) -> some View {
        let shown = Array(images.prefix(MediaPolicy.maxMediaItems))
        let rows = MediaPolicy.galleryRows(count: shown.count)
        let height = MediaPolicy.galleryRowHeight(count: shown.count)
        let starts = rows.indices.map { rows[..<$0].reduce(0, +) }
        return VStack(alignment: .leading, spacing: MediaPolicy.gridGap) {
            ForEach(rows.indices, id: \.self) { row in
                HStack(spacing: MediaPolicy.gridGap) {
                    ForEach(0..<rows[row], id: \.self) { column in
                        galleryTile(shown[starts[row] + column],
                                    size: CGSize(width: MediaPolicy.galleryTileWidth(columns: rows[row]), height: height))
                    }
                }
            }
        }
        .frame(width: MediaPolicy.cardWidth, alignment: .leading)
    }

    private func galleryTile(_ item: ImageItem, size: CGSize) -> some View {
        let state = states[item.url] ?? .loading
        return MediaImageView(state: state, alt: item.alt, sourcePage: item.sourcePage)
            .frame(width: size.width, height: size.height)
            .clipped()
            .mediaTileLabel(state: state, fallback: item.alt ?? card.title)
    }
}

// MARK: - Video card

/// Whole video card: title bar, a 16:9 area (player, thumbnail + play, or fallback), footer row.
@MainActor
public struct VideoCardView<Player: View>: View {
    private let card: VideoCard
    private let thumbnail: MediaImageState?
    private let phase: VideoPhase
    private let onPlay: () -> Void
    private let player: Player

    @State private var hovering = false

    public init(card: VideoCard, thumbnail: MediaImageState?, phase: VideoPhase,
                onPlay: @escaping () -> Void, @ViewBuilder player: () -> Player) {
        self.card = card
        self.thumbnail = thumbnail
        self.phase = phase
        self.onPlay = onPlay
        self.player = player()
    }

    public var body: some View {
        MediaChrome(tag: card.youtubeID != nil ? "YOUTUBE" : "VIDEO", title: card.title, hasFooter: true,
                    media: { area }, footer: { footerRow })
    }

    /// Only YouTube plays inside the card; a direct video file opens in the browser.
    private var playsInline: Bool { card.youtubeID != nil }

    @ViewBuilder
    private var area: some View {
        ZStack {
            Color.black
            if !playsInline {
                thumbnailView
            } else {
                switch phase {
                case .playing:
                    player
                        .frame(width: MediaPolicy.videoAreaSize.width, height: MediaPolicy.videoAreaSize.height)
                case .starting:
                    // The player loads underneath; this cover hides its first paint until a frame plays.
                    player
                        .frame(width: MediaPolicy.videoAreaSize.width, height: MediaPolicy.videoAreaSize.height)
                    poster.opacity(0.4)
                    startingMark
                case .idle:
                    poster
                    VStack(spacing: 10) {
                        playButton
                        if let posterCaption {
                            Text(posterCaption)
                                .font(Theme.fontSmall)
                                .foregroundStyle(Color.white.opacity(0.72))
                        }
                    }
                case .failed:
                    poster.opacity(0.4)
                    Text("Can't play here. Open it in your browser.")
                        .font(Theme.fontBody)
                        .foregroundStyle(.white)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
            }
        }
        .frame(width: MediaPolicy.videoAreaSize.width, height: MediaPolicy.videoAreaSize.height)
        .clipped()
    }

    /// The loaded thumbnail, else a plain tile with no text (the play button and its caption sit on it).
    @ViewBuilder
    private var poster: some View {
        if case .loaded(let image) = thumbnail {
            MediaImageView(state: .loaded(image), alt: card.title)
                .accessibilityLabel(card.title)
        } else {
            Rectangle().fill(Color.white.opacity(0.06))
        }
    }

    /// Below the play button while the thumbnail is not there.
    private var posterCaption: String? {
        switch thumbnail {
        case .loading?: return "Loading preview"
        case .failed?: return "No preview"
        default: return nil
        }
    }

    private var startingMark: some View {
        VStack(spacing: 8) {
            if !Theme.reduceMotion {
                ProgressView().controlSize(.small)
            }
            Text("Starting video")
                .font(Theme.fontSmall)
                .foregroundStyle(Color.white.opacity(0.85))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Starting video")
    }

    @ViewBuilder
    private var thumbnailView: some View {
        if let thumbnail {
            MediaImageView(state: thumbnail, alt: card.title)
                .mediaTileLabel(state: thumbnail, fallback: card.title)
        } else {
            filmTile
        }
    }

    /// No thumbnail at all: film symbol and the title on the tile fill.
    private var filmTile: some View {
        ZStack {
            Rectangle().fill(Color.white.opacity(0.06))
            VStack(spacing: 8) {
                Image(systemName: "film")
                    .font(Theme.fontTitle)
                    .foregroundStyle(Color.white.opacity(0.5))
                    .accessibilityHidden(true)
                Text(card.title)
                    .font(Theme.fontSmall)
                    .foregroundStyle(Color.white.opacity(0.72))
                    .lineLimit(1)
                    .padding(.horizontal, 6)
            }
        }
        .accessibilityHidden(true) // the header already reads the title
    }

    /// 56 pt circle; the only hover treatment is the ring opacity (no scale, no layout move).
    private var playButton: some View {
        Button(action: onPlay) {
            ZStack {
                Circle().fill(Theme.cardBackground)
                Circle().strokeBorder(Theme.accent.opacity(hovering ? 1 : 0.8), lineWidth: 2)
                Image(systemName: "play.fill")
                    .font(Theme.fontTitle)
                    .foregroundStyle(.white)
                    .offset(x: 2) // optical centring of the triangle
            }
            .frame(width: 56, height: 56)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel("Play \(card.title)")
    }

    private var footerRow: some View {
        HStack(spacing: 8) {
            Spacer(minLength: 8)
            if let url = card.browserURL, isSafeHTTPSURL(url.absoluteString) {
                Button {
                    // Only https, re-checked here (payload is untrusted).
                    if let url = card.browserURL, isSafeHTTPSURL(url.absoluteString) {
                        NSWorkspace.shared.open(url)
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text("Open in browser")
                            .font(Theme.fontSmall)
                            .foregroundStyle(Color.white.opacity(0.72))
                        Image(systemName: "arrow.up.right")
                            .font(Theme.fontSmall)
                            .foregroundStyle(Theme.accent)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .trailing)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Open \(url.host ?? card.title) in browser")
            }
        }
        .padding(.horizontal, 14)
        .frame(width: MediaPolicy.cardWidth, height: MediaPolicy.footerHeight)
    }
}

/// VideoCardView with no inline player (the overlay wires players separately).
public extension VideoCardView where Player == EmptyView {
    init(card: VideoCard, thumbnail: MediaImageState?, phase: VideoPhase, onPlay: @escaping () -> Void) {
        self.init(card: card, thumbnail: thumbnail, phase: phase, onPlay: onPlay, player: { EmptyView() })
    }
}
