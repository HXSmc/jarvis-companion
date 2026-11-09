import SwiftUI
import AppKit
import Foundation

// MARK: - Card chrome

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
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let meta {
                Text(meta)
                    .font(Theme.fontSmall)
                    .foregroundStyle(Color.white.opacity(0.72))
                    .lineLimit(1)
            }
            Text(title)
                .font(Theme.cardTitle)
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

    public init(card: HUDCard) {
        self.card = card
    }

    public var body: some View {
        switch card {
        case .info(let p):
            CardChrome(meta: nil, title: p.title, width: 300) { InfoBody(lines: p.body) }
        case .stat(let p):
            CardChrome(meta: nil, title: p.title, width: 300) { StatBody(value: p.value, label: p.label) }
        case .bar(let p):
            CardChrome(meta: nil, title: p.title, width: 360) { BarBody(card: p) }
        case .flow(let p):
            CardChrome(meta: nil, title: p.title, width: 360) { FlowBody(card: p) }
        case .link(let p):
            CardChrome(meta: LinkBody.host(of: p), title: p.title, width: 300) { LinkBody(card: p) }
        case .image(let p):
            CardChrome(meta: nil, title: p.title, width: 300) { ImageBody(card: p) }
        }
    }
}

// MARK: - Bodies

@MainActor
struct InfoBody: View {
    let lines: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(Theme.fontBody)
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

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(Theme.fontDisplay)
                .foregroundStyle(.white)
                .lineLimit(1)
            Text(label)
                .font(Theme.fontSmall)
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

    private var maxValue: Double {
        max(card.items.map(\.value).max() ?? 1, 0.001)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(card.items.enumerated()), id: \.offset) { _, item in
                HStack(spacing: 8) {
                    Text(item.label)
                        .font(Theme.fontSmall)
                        .foregroundStyle(Color.white.opacity(0.72))
                        .lineLimit(1)
                        .frame(width: 92, alignment: .leading)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Rectangle().fill(Color.white.opacity(0.10))
                            Rectangle().fill(Theme.accent.opacity(0.85))
                                .frame(width: geo.size.width * CGFloat(item.value / maxValue))
                        }
                    }
                    .frame(height: 10)
                    Text(barValueText(item.value))
                        .font(Theme.fontSmall.monospacedDigit())
                        .foregroundStyle(Color.white.opacity(0.85))
                        .frame(width: 56, alignment: .trailing)
                }
            }
            if let caption = card.caption, !caption.isEmpty {
                Text(caption)
                    .font(Theme.fontSmall)
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

@MainActor
struct ImageBody: View {
    let card: ImageCard

    /// Remote images load only after an explicit click (payload is untrusted).
    @State private var loadRequested = false

    private var safeURL: URL? {
        guard let urlString = card.url, isSafeHTTPSURL(urlString) else { return nil }
        return URL(string: urlString)
    }

    var body: some View {
        Group {
            if let url = safeURL {
                if loadRequested {
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .success(let image):
                            image.resizable().aspectRatio(contentMode: .fill)
                        case .failure:
                            tile(text: "Could not load image")
                        default:
                            tile(text: "Loading")
                        }
                    }
                } else {
                    loadTile
                }
            } else if let fileID = card.fileID, !fileID.isEmpty {
                tile(text: "Saved image")
            } else {
                tile(text: "No image")
            }
        }
        .frame(height: 150)
        .frame(maxWidth: .infinity)
        .clipped()
        .overlay(RoundedRectangle(cornerRadius: 2).strokeBorder(Color.white.opacity(0.15), lineWidth: 1))
    }

    private var loadTile: some View {
        VStack(spacing: 8) {
            if let host = safeURL?.host {
                Text(host)
                    .font(Theme.fontSmall)
                    .foregroundStyle(Color.white.opacity(0.72))
                    .lineLimit(1)
            }
            Button {
                loadRequested = true
            } label: {
                Text("Load image")
                    .font(Theme.fontSmall)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    // White on red is ~3.5:1; keep the text on dark and let the accent be the border.
                    .background(RoundedRectangle(cornerRadius: Theme.radiusShape).fill(Color.white.opacity(0.12)))
                    .overlay(RoundedRectangle(cornerRadius: Theme.radiusShape).strokeBorder(Theme.accent, lineWidth: 1))
            }
            .buttonStyle(.plain)
        }
    }

    private func tile(text: String) -> some View {
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

// MARK: - Flow body (canvas + node boxes; layout from FlowLayout)

@MainActor
struct FlowBody: View {
    let card: FlowCard

    var body: some View {
        GeometryReader { geo in
            let rects = FlowLayout.layout(nodes: card.nodes, edges: card.edges, size: geo.size)
            ZStack(alignment: .topLeading) {
                Canvas { ctx, _ in
                    for edge in card.edges {
                        guard let f = rects[edge.from], let t = rects[edge.to] else { continue }
                        drawArrow(&ctx, from: f, to: t)
                        if let label = edge.label, !label.isEmpty {
                            let mid = CGPoint(x: (f.midX + t.midX) / 2 + 6, y: (f.midY + t.midY) / 2)
                            ctx.draw(Text(label)
                                .font(Theme.fontMicro)
                                .foregroundColor(.white.opacity(0.72)), at: mid, anchor: .leading)
                        }
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

    /// Rows of 34 pt nodes with 22 pt gaps (FlowLayout's numbers), so short flows stay compact.
    private var height: CGFloat {
        let rows = CGFloat(max(Set(FlowLayout.depths(nodes: card.nodes, edges: card.edges).values).count, 1))
        return min(rows * 34 + (rows - 1) * 22, 300)
    }

    private func nodeBox(_ node: FlowNode) -> some View {
        Text(node.text)
            .font(Theme.fontSmall)
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
