import SwiftUI
import AppKit
import Foundation

// MARK: - Card chrome

/// Theme type sizes rebuilt at a scale (Font carries no size); the enlarged layer re-lays out
/// with these instead of scaling a bitmap.
enum ScaledType {
    static func micro(_ s: CGFloat) -> Font { .system(size: 11 * s, weight: .medium, design: .monospaced) }
    static func small(_ s: CGFloat) -> Font { .system(size: 12 * s) }
    static func body(_ s: CGFloat) -> Font { .system(size: 13 * s) }
    static func title(_ s: CGFloat) -> Font { .system(size: 17 * s, weight: .bold) }
    static func display(_ s: CGFloat) -> Font { .system(size: 44 * s, weight: .bold).monospacedDigit() }
}

/// Two short accent corner marks, top-left.
@MainActor
struct CornerTicks: View {
    var body: some View {
        ZStack(alignment: .topLeading) {
            Path { p in
                p.move(to: CGPoint(x: 0, y: 8))
                p.addLine(to: CGPoint(x: 0, y: 0))
                p.addLine(to: CGPoint(x: 8, y: 0))
            }
            .stroke(Theme.accent, lineWidth: 1)
            Path { p in
                p.move(to: CGPoint(x: 4, y: 12))
                p.addLine(to: CGPoint(x: 4, y: 4))
                p.addLine(to: CGPoint(x: 12, y: 4))
            }
            .stroke(Theme.accent.opacity(0.5), lineWidth: 1)
        }
        .allowsHitTesting(false)
    }
}

/// Dark translucent card: optional meta line, bold title, 1pt border, corner ticks.
@MainActor
struct CardChrome<Content: View>: View {
    /// Real information (link host); nil for cards with nothing to say.
    let meta: String?
    let title: String
    let width: CGFloat?
    /// Enlarged-layer font scale (1 = the fixed Theme sizes).
    var fontScale: CGFloat = 1
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let meta {
                Text(meta)
                    .font(ScaledType.small(fontScale))
                    .foregroundStyle(Color.white.opacity(0.72))
                    .lineLimit(1)
            }
            Text(title)
                .font(ScaledType.title(fontScale))
                .foregroundStyle(.white)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            content()
        }
        .padding(14)
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
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Dispatch

@MainActor
public struct CardView: View {
    public let card: HUDCard
    /// Width override for the enlarged layer; nil = the card's fixed width.
    var width: CGFloat?
    /// Font scale for the enlarged layer, capped at CardSize.fontScaleCap.
    var enlargedFontScale: CGFloat

    public init(card: HUDCard, width: CGFloat? = nil, enlargedFontScale: CGFloat = 1) {
        self.card = card
        self.width = width
        self.enlargedFontScale = min(enlargedFontScale, CardSize.fontScaleCap)
    }

    /// Width the card kind is built at; the enlarged layer scales fonts from it.
    static func baseWidth(of card: HUDCard) -> CGFloat {
        switch card {
        case .bar, .flow: return 360
        case .image, .video: return MediaPolicy.cardWidth
        default: return 300
        }
    }

    /// Font scale of a card drawn at `width`: its base width ratio, capped (sharp text re-laid
    /// out, never a scaled bitmap).
    public static func fontScale(card: HUDCard, width: CGFloat) -> CGFloat {
        if case .flow(let p) = card { return min(width / baseWidth(of: card), FlowFit.scale(p, width: width)) }
        return min(width / baseWidth(of: card), CardSize.fontScaleCap)
    }

    public var body: some View {
        switch card {
        case .info(let p):
            CardChrome(meta: nil, title: p.title, width: width ?? 300, fontScale: enlargedFontScale) {
                InfoBody(lines: p.body, scale: enlargedFontScale)
            }
        case .stat(let p):
            CardChrome(meta: nil, title: p.title, width: width ?? 300, fontScale: enlargedFontScale) {
                StatBody(value: p.value, label: p.label, scale: enlargedFontScale)
            }
        case .bar(let p):
            CardChrome(meta: nil, title: p.title, width: width ?? 360, fontScale: enlargedFontScale) {
                BarBody(card: p, scale: enlargedFontScale)
            }
        case .flow(let p):
            CardChrome(meta: nil, title: p.title, width: width ?? 360, fontScale: enlargedFontScale) {
                FlowBody(card: p, scale: enlargedFontScale)
            }
        case .link(let p):
            CardChrome(meta: LinkBody.host(of: p), title: p.title, width: width ?? 300,
                       fontScale: enlargedFontScale) {
                LinkBody(card: p)
            }
        case .image(let p):
            ImageCardView(card: p, width: width, scale: enlargedFontScale)
        case .video(let p):
            VideoCardView(card: p, width: width, scale: enlargedFontScale,
                          thumbnail: p.youtubeID != nil || p.thumbnailURL != nil ? .loading : nil,
                          phase: .idle,
                          onPlay: {})
        }
    }
}

// MARK: - Bodies

@MainActor
struct InfoBody: View {
    let lines: [String]
    var scale: CGFloat = 1

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(ScaledType.body(scale))
                    .foregroundStyle(Color.white.opacity(0.85))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

@MainActor
struct StatBody: View {
    let value: String
    let label: String
    var scale: CGFloat = 1

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(ScaledType.display(scale))
                .foregroundStyle(.white)
                .lineLimit(1)
            Text(label)
                .font(ScaledType.small(scale))
                .foregroundStyle(Color.white.opacity(0.72))
        }
    }
}

private func barValueText(_ v: Double) -> String {
    v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v)
}

@MainActor
struct BarBody: View {
    let card: BarCard
    var scale: CGFloat = 1

    private var maxValue: Double {
        max(card.items.map(\.value).max() ?? 1, 0.001)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(card.items.enumerated()), id: \.offset) { _, item in
                HStack(spacing: 8) {
                    Text(item.label)
                        .font(ScaledType.small(scale))
                        .foregroundStyle(Color.white.opacity(0.72))
                        .lineLimit(1)
                        .frame(width: 92 * scale, alignment: .leading)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Rectangle().fill(Color.white.opacity(0.10))
                            Rectangle().fill(Theme.accent.opacity(0.85))
                                .frame(width: geo.size.width * CGFloat(item.value / maxValue))
                        }
                    }
                    .frame(height: 10 * scale)
                    Text(barValueText(item.value))
                        .font(ScaledType.small(scale).monospacedDigit())
                        .foregroundStyle(Color.white.opacity(0.85))
                        .frame(width: 56 * scale, alignment: .trailing)
                }
            }
            if let caption = card.caption, !caption.isEmpty {
                Text(caption)
                    .font(ScaledType.small(scale))
                    .foregroundStyle(Color.white.opacity(0.72))
            }
        }
    }
}

@MainActor
struct LinkBody: View {
    let card: LinkCard

    static func host(of card: LinkCard) -> String {
        URL(string: card.url)?.host ?? card.url
    }

    private var host: String { Self.host(of: card) }

    @State private var hovering = false

    var body: some View {
        Button {
            // Only https, re-checked here (payload is untrusted).
            if isSafeHTTPSURL(card.url), let url = URL(string: card.url) {
                NSWorkspace.shared.open(url)
            }
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                if let source = card.source, !source.isEmpty, source != host {
                    Text(source)
                        .font(Theme.fontSmall)
                        .foregroundStyle(Color.white.opacity(0.72))
                        .lineLimit(1)
                }
                HStack(spacing: 4) {
                    Text("Open link")
                        .font(Theme.fontSmall)
                        .foregroundStyle(.white)
                    Image(systemName: "arrow.up.right")
                        .font(Theme.fontSmall)
                        .foregroundStyle(Theme.accent)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
            .background(Color.white.opacity(hovering ? 0.06 : 0))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel("Open \(host)")
    }
}

// MARK: - Flow body (canvas + node boxes; layout from FlowLayout)

@MainActor
struct FlowBody: View {
    let card: FlowCard
    var scale: CGFloat = 1

    var body: some View {
        GeometryReader { geo in
            let rects = Self.rects(card: card, size: geo.size, scale: scale)
            let labels = Self.labels(card: card, rects: rects, scale: scale)
            ZStack(alignment: .topLeading) {
                Canvas { ctx, _ in
                    for edge in card.edges {
                        guard let f = rects[edge.from], let t = rects[edge.to] else { continue }
                        drawArrow(&ctx, from: f, to: t)
                    }
                    for (label, spot) in labels {
                        ctx.draw(Text(label)
                            .font(ScaledType.micro(scale))
                            .foregroundColor(.white.opacity(0.72)), at: spot, anchor: .leading)
                    }
                }
                ForEach(Array(card.nodes.enumerated()), id: \.offset) { _, node in
                    if let r = rects[node.id] {
                        nodeBox(node)
                            .frame(width: r.width, height: r.height)
                            .position(x: r.midX, y: r.midY)
                    }
                }
            }
        }
        .frame(height: height)
    }

    /// Node rects at `scale`: FlowLayout's grid (34 pt rows, 22 pt gaps) laid out in the unscaled
    /// space, then scaled, so a node grows with its font (Mac live run 1: rows stayed 34 pt under a
    /// 2.5x font and every label was cut). The flow's font scale stays under the width ratio, so an
    /// enlarged node has more room per word than the tile.
    static func rects(card: FlowCard, size: CGSize, scale: CGFloat) -> [String: CGRect] {
        let s = max(scale, 0.01)
        let unscaled = FlowLayout.layout(nodes: card.nodes, edges: card.edges,
                                         size: CGSize(width: size.width / s, height: size.height / s))
        return unscaled.mapValues { CGRect(x: $0.minX * s, y: $0.minY * s, width: $0.width * s, height: $0.height * s) }
    }

    /// Edge labels with a free spot (FlowEdgeLabels): never over each other or a node.
    static func labels(card: FlowCard, rects: [String: CGRect], scale: CGFloat) -> [(String, CGPoint)] {
        let edges = card.edges.compactMap { e -> (String, CGRect, CGRect)? in
            guard let label = e.label, !label.isEmpty, let f = rects[e.from], let t = rects[e.to] else { return nil }
            return (label, f, t)
        }
        let spots = FlowEdgeLabels.place(edges.map { ($0.1, $0.2, FlowEdgeLabels.size($0.0, scale: scale)) },
                                         nodes: Array(rects.values), gap: 6 * scale)
        return zip(edges, spots).compactMap { e, spot in spot.map { (e.0, $0) } }
    }

    /// Rows of 34 pt nodes with 22 pt gaps (FlowLayout's numbers), so short flows stay compact.
    private var height: CGFloat {
        let rows = CGFloat(max(Set(FlowLayout.depths(nodes: card.nodes, edges: card.edges).values).count, 1))
        return min((rows * 34 + (rows - 1) * 22) * scale, 300 * scale)
    }

    private func nodeBox(_ node: FlowNode) -> some View {
        Text(node.text)
            .font(ScaledType.small(scale))
            .foregroundStyle(Color.white.opacity(0.85))
            .lineLimit(2)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 6)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(RoundedRectangle(cornerRadius: Theme.radiusCard).fill(Color.white.opacity(0.08)))
    }

    /// Distance from a rect's centre to its border along the unit vector (ux, uy).
    private func edgeDistance(_ r: CGRect, _ ux: CGFloat, _ uy: CGFloat) -> CGFloat {
        let hx = abs(ux) > 0.0001 ? (r.width / 2) / abs(ux) : .greatestFiniteMagnitude
        let hy = abs(uy) > 0.0001 ? (r.height / 2) / abs(uy) : .greatestFiniteMagnitude
        return min(hx, hy)
    }

    private func drawArrow(_ ctx: inout GraphicsContext, from f: CGRect, to t: CGRect) {
        let c0 = CGPoint(x: f.midX, y: f.midY)
        let c1 = CGPoint(x: t.midX, y: t.midY)
        let dx = c1.x - c0.x, dy = c1.y - c0.y
        let len = max((dx * dx + dy * dy).squareRoot(), 0.001)
        let ux = dx / len, uy = dy / len
        // Border to border, so the line never runs through a node's text.
        let s = CGPoint(x: c0.x + ux * (edgeDistance(f, ux, uy) + 2), y: c0.y + uy * (edgeDistance(f, ux, uy) + 2))
        let e = CGPoint(x: c1.x - ux * (edgeDistance(t, ux, uy) + 2), y: c1.y - uy * (edgeDistance(t, ux, uy) + 2))
        var line = Path()
        line.move(to: s)
        line.addLine(to: e)
        ctx.stroke(line, with: .color(.white.opacity(0.6)), lineWidth: 1)
        let px = -uy, py = ux
        var head = Path()
        head.move(to: e)
        head.addLine(to: CGPoint(x: e.x - ux * 7 + px * 4, y: e.y - uy * 7 + py * 4))
        head.addLine(to: CGPoint(x: e.x - ux * 7 - px * 4, y: e.y - uy * 7 - py * 4))
        head.closeSubpath()
        ctx.fill(head, with: .color(.white.opacity(0.7)))
    }
}
