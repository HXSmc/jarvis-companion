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
/// `width`/`titleScale` are the enlarged layer's overrides; the defaults draw the tile.
@MainActor
private struct MediaChrome<Media: View, Footer: View>: View {
    let tag: String
    let title: String
    var width: CGFloat = MediaPolicy.cardWidth
    var titleScale: CGFloat = 1
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
                    .frame(width: width, height: MediaPolicy.footerHeight * max(titleScale, 1))
            }
        }
        .frame(width: width, alignment: .leading)
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
                .font(ScaledType.title(titleScale))
                .foregroundStyle(.white)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 8)
            Text(tag)
                .font(ScaledType.micro(titleScale))
                .foregroundStyle(Theme.accent)
                .fixedSize()
        }
        .padding(.horizontal, 14)
        .frame(width: width, height: MediaPolicy.titleBarHeight * max(titleScale, 1), alignment: .leading)
        .accessibilityAddTraits(.isHeader)
    }
}

/// Media cards without a footer row (image cards).
extension MediaChrome where Footer == EmptyView {
    init(tag: String, title: String, width: CGFloat = MediaPolicy.cardWidth, titleScale: CGFloat = 1,
         @ViewBuilder media: @escaping () -> Media) {
        self.init(tag: tag, title: title, width: width, titleScale: titleScale,
                  hasFooter: false, media: media, footer: { EmptyView() })
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
        .animation(Theme.reduceMotion ? nil : .easeOut(duration: Theme.motionShort), value: isLoaded)
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

/// Whole image card (title bar + single image or gallery grid), sized by MediaPolicy. The
/// enlarged layer overrides the width and lifts the single-image height cap (maxCardHeight
/// bounds tiles only; maxDecodedSide still bounds the decode, the window bounds the layer).
@MainActor
public struct ImageCardView: View {
    private let card: ImageCard
    private let states: [String: MediaImageState]
    /// Enlarged layer: card width override, type scale, single-image height bound.
    private let width: CGFloat?
    private let scale: CGFloat
    private let maxHeight: CGFloat?

    public init(card: ImageCard, states: [String: MediaImageState] = [:],
                width: CGFloat? = nil, scale: CGFloat = 1, maxHeight: CGFloat? = nil) {
        self.card = card
        self.states = states
        self.width = width
        self.scale = scale
        self.maxHeight = maxHeight
    }

    public var body: some View {
        MediaChrome(tag: tag, title: card.title, width: width ?? MediaPolicy.cardWidth,
                    titleScale: scale, media: { mediaArea })
    }

    /// "n FRAMES" for a gallery (capped by policy), "IMAGE" otherwise.
    private var tag: String {
        let count = min(card.images.count, MediaPolicy.maxMediaItems)
        return count >= 2 ? "\(count) FRAMES" : "IMAGE"
    }

    /// The no-image tile keeps the policy 16:9 area for tiles, the enlarged width's own for the
    /// enlarged layer.
    private var placeholderSize: CGSize {
        guard let width else { return MediaPolicy.videoAreaSize }
        return CGSize(width: width, height: width * MediaPolicy.videoAreaSize.height / MediaPolicy.videoAreaSize.width)
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
                .frame(width: placeholderSize.width, height: placeholderSize.height)
        }
    }

    /// The loaded area takes its aspect from the pixels; every other state keeps the 16:9 tile.
    private func singleImage(_ item: ImageItem) -> some View {
        let state = states[item.url] ?? .loading
        let size: CGSize
        if case .loaded(let image) = state {
            if let width {
                // Enlarged: the decoded image's aspect at the card width, height cap lifted.
                size = CGSize(width: width,
                              height: width * CGFloat(image.height) / CGFloat(max(image.width, 1)))
                if let maxHeight, size.height > maxHeight {
                    size = CGSize(width: maxHeight * CGFloat(image.width) / CGFloat(max(image.height, 1)),
                                  height: maxHeight)
                }
            } else {
                size = MediaPolicy.imageAreaSize(pixelWidth: image.width, pixelHeight: image.height)
            }
        } else {
            size = placeholderSize
        }
        return MediaImageView(state: state, alt: item.alt, sourcePage: item.sourcePage)
            .frame(width: size.width, height: size.height)
            .clipped()
            .mediaTileLabel(state: state, fallback: item.alt ?? card.title)
    }

    /// Full rows from MediaPolicy.galleryRows (no empty cell), one row height, policy gaps; the
    /// enlarged layer scales the tile sizes by its width ratio (images scale, no tile text to blur).
    private func gallery(_ images: [ImageItem]) -> some View {
        let shown = Array(images.prefix(MediaPolicy.maxMediaItems))
        let rows = MediaPolicy.galleryRows(count: shown.count)
        let height = MediaPolicy.galleryRowHeight(count: shown.count)
        let starts = rows.indices.map { rows[..<$0].reduce(0, +) }
        let ratio = (width ?? MediaPolicy.cardWidth) / MediaPolicy.cardWidth
        return VStack(alignment: .leading, spacing: MediaPolicy.gridGap * max(ratio, 1)) {
            ForEach(rows.indices, id: \.self) { row in
                HStack(spacing: MediaPolicy.gridGap * max(ratio, 1)) {
                    ForEach(0..<rows[row], id: \.self) { column in
                        galleryTile(shown[starts[row] + column],
                                    size: CGSize(width: MediaPolicy.galleryTileWidth(columns: rows[row]) * ratio,
                                                 height: height * ratio))
                    }
                }
            }
        }
        .frame(width: width ?? MediaPolicy.cardWidth, alignment: .leading)
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
/// `width`/`scale` are the enlarged layer's overrides; the defaults draw the tile.
@MainActor
public struct VideoCardView<Player: View>: View {
    private let card: VideoCard
    private let width: CGFloat?
    private let scale: CGFloat
    private let thumbnail: MediaImageState?
    private let phase: VideoPhase
    private let onPlay: () -> Void
    private let player: Player

    @State private var hovering = false

    public init(card: VideoCard, width: CGFloat? = nil, scale: CGFloat = 1, thumbnail: MediaImageState?,
                phase: VideoPhase, onPlay: @escaping () -> Void, @ViewBuilder player: () -> Player) {
        self.card = card
        self.width = width
        self.scale = scale
        self.thumbnail = thumbnail
        self.phase = phase
        self.onPlay = onPlay
        self.player = player()
    }

    public var body: some View {
        MediaChrome(tag: card.youtubeID != nil ? "YOUTUBE" : "VIDEO", title: card.title,
                    width: width ?? MediaPolicy.cardWidth, titleScale: scale, hasFooter: true,
                    media: { area }, footer: { footerRow })
    }

    /// Only YouTube plays inside the card; a direct video file opens in the browser.
    private var playsInline: Bool { card.youtubeID != nil }

    /// 16:9 area: the policy size for tiles, the enlarged width's own for the enlarged layer.
    private var areaSize: CGSize {
        guard let width else { return MediaPolicy.videoAreaSize }
        return CGSize(width: width, height: width * MediaPolicy.videoAreaSize.height / MediaPolicy.videoAreaSize.width)
    }

    @ViewBuilder
    private var area: some View {
        ZStack {
            Color.black
            if !playsInline {
                thumbnailView
            } else {
                // Built once for .starting and .playing (one identity), so the web view is not
                // recreated, and the video does not restart, when the cover goes away.
                if phase == .starting || phase == .playing {
                    player
                        .frame(width: areaSize.width, height: areaSize.height)
                }
                switch phase {
                case .playing:
                    EmptyView()
                case .starting:
                    // Opaque cover: nothing of the loading page shows until a frame plays.
                    Color.black
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
        .frame(width: areaSize.width, height: areaSize.height)
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
        .frame(width: width ?? MediaPolicy.cardWidth, height: MediaPolicy.footerHeight * max(scale, 1))
    }
}

/// VideoCardView with no inline player (the overlay wires players separately).
public extension VideoCardView where Player == EmptyView {
    init(card: VideoCard, width: CGFloat? = nil, scale: CGFloat = 1,
         thumbnail: MediaImageState?, phase: VideoPhase, onPlay: @escaping () -> Void) {
        self.init(card: card, width: width, scale: scale,
                  thumbnail: thumbnail, phase: phase, onPlay: onPlay, player: { EmptyView() })
    }
}
