import SwiftUI

/// Style tokens for the HUD layer: one cool cyan accent, the rest white on dark
/// translucent panels with thin 1 pt borders and small corner ticks.
private let hudLine = Color(red: 0.62, green: 0.93, blue: 1.0)    // border, ticks
private let hudAccent = Color(red: 0.45, green: 0.88, blue: 1.0)  // the one accent
private let hudBody = Color.white.opacity(0.85)

/// One rendered HUD card. Fills whatever frame it is given (the layer's slot), tall
/// content scrolls inside the panel.
struct HUDCardView: View {
    let card: HUDCard

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 8) {
                Text(card.microLabel)
                    .font(.system(size: 9, weight: .semibold, design: .monospaced))
                    .foregroundColor(hudAccent)
                    .kerning(1.2)
                Text(card.title)
                    .font(.system(size: 16, weight: .bold))
                    .fontWidth(.condensed)
                    .foregroundColor(.white)
                    .lineLimit(2)
                content
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 4, style: .continuous).fill(.ultraThinMaterial)
                RoundedRectangle(cornerRadius: 4, style: .continuous).fill(Color.black.opacity(0.6))
            }
        )
        .overlay(RoundedRectangle(cornerRadius: 4, style: .continuous).strokeBorder(hudLine.opacity(0.55), lineWidth: 1))
        .overlay(HUDCornerTicks())
        .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    @ViewBuilder private var content: some View {
        switch card.type {
        case "info": infoBody
        case "stat": statBody
        case "bar": barBody
        case "flow": flowBody
        case "link": linkBody
        case "image": imageBody
        default: EmptyView()
        }
    }

    private var infoBody: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array((card.body ?? []).enumerated()), id: \.offset) { _, line in
                Text(line)
                    .font(.system(size: 13))
                    .foregroundColor(hudBody)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var statBody: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(card.value ?? "")
                .font(.system(size: 30, weight: .heavy))
                .fontWidth(.condensed)
                .foregroundColor(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
            if let label = card.statLabel, !label.isEmpty {
                Text(label)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(hudBody)
                    .lineLimit(2)
            }
        }
    }

    private var barBody: some View {
        let items = card.items ?? []
        let peak = max(items.map(\.value).max() ?? 1, 0.001)
        return VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                HStack(spacing: 6) {
                    Text(item.label)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(hudBody)
                        .lineLimit(1)
                        .frame(width: 74, alignment: .leading)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.white.opacity(0.15))
                            Capsule().fill(hudAccent)
                                .frame(width: geo.size.width * CGFloat(item.value / peak))
                        }
                    }
                    .frame(height: 6)
                    Text(Self.shortNumber(item.value))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(hudBody)
                }
            }
            if let caption = card.caption, !caption.isEmpty {
                Text("sourced from \(caption)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundColor(hudBody.opacity(0.7))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
        }
    }

    private var flowBody: some View {
        let nodes = card.nodes ?? []
        let extra = extraEdges()
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(nodes.enumerated()), id: \.offset) { i, node in
                if i > 0 { connector(from: nodes[i - 1], to: node) }
                Text(node.text)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(.white)
                    .lineLimit(2)
                    .padding(.horizontal, 8).padding(.vertical, 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Color.white.opacity(0.35), lineWidth: 1))
            }
            if !extra.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(extra.enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(hudBody.opacity(0.7))
                            .lineLimit(1)
                    }
                }
                .padding(.top, 8)
            }
        }
    }

    /// Arrow between consecutive nodes, with the edge label when there is one.
    private func connector(from a: HUDCard.FlowNode, to b: HUDCard.FlowNode) -> some View {
        let edge = card.edges?.first { $0.from == a.id && $0.to == b.id }
        return HStack(spacing: 4) {
            Image(systemName: "arrow.down")
                .font(.system(size: 9, weight: .semibold))
                .foregroundColor(hudAccent)
            if let label = edge?.label, !label.isEmpty {
                Text(label)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(hudBody.opacity(0.7))
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 3).padding(.leading, 10)
        .accessibilityHidden(true)
    }

    /// Edges that are not between consecutive nodes, as "A -> B" text lines.
    private func extraEdges() -> [String] {
        let nodes = card.nodes ?? []
        let consecutive = Set(zip(nodes, nodes.dropFirst()).map { "\($0.0.id)>\($0.1.id)" })
        var out: [String] = []
        for e in card.edges ?? [] where !consecutive.contains("\(e.from)>\(e.to)") {
            let from = nodes.first { $0.id == e.from }?.text ?? e.from
            let to = nodes.first { $0.id == e.to }?.text ?? e.to
            out.append(e.label.isEmpty ? "\(from) → \(to)" : "\(from) → \(to) · \(e.label)")
        }
        return out
    }

    private var linkBody: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let host = card.url?.host {
                Text(host)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(hudBody)
                    .lineLimit(1)
            }
            if let url = card.url {
                Link(destination: url) {
                    Text("Open")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundColor(hudAccent)
                        .padding(.horizontal, 16)
                        .frame(minHeight: 44)  // 44 pt hit area
                        .frame(maxWidth: .infinity)
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(hudAccent.opacity(0.8), lineWidth: 1))
                }
            }
        }
    }

    private var imageBody: some View {
        Group {
            if let url = card.url {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().scaledToFill()
                    default:
                        ZStack {
                            Rectangle().fill(Color.white.opacity(0.08))  // quiet placeholder
                            Image(systemName: "photo").font(.system(size: 16)).foregroundColor(hudBody.opacity(0.6))
                        }
                    }
                }
                .frame(height: 110)
                .frame(maxWidth: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.2), lineWidth: 1))
            } else {
                // file_id only: the picture is in the session files, nothing to fetch here.
                HStack(spacing: 6) {
                    Image(systemName: "photo").font(.system(size: 14))
                    Text("Image in the session files").font(.system(size: 12))
                }
                .foregroundColor(hudBody)
                .padding(.horizontal, 10).padding(.vertical, 8)
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.25), lineWidth: 1))
            }
        }
    }

    // MARK: helpers

    static func shortNumber(_ v: Double) -> String {
        v.truncatingRemainder(dividingBy: 1) == 0 ? String(format: "%.0f", v) : String(format: "%.1f", v)
    }

    private var accessibilityText: String {
        var s = "\(card.microLabel), \(card.title)"
        switch card.type {
        case "info": s += ", " + (card.body ?? []).joined(separator: ", ")
        case "stat":
            s += ", \(card.value ?? "")"
            if let label = card.statLabel { s += ", \(label)" }
        case "bar": s += ", " + (card.items ?? []).map { "\($0.label) \(Self.shortNumber($0.value))" }.joined(separator: ", ")
        case "flow": s += ", " + (card.nodes ?? []).map(\.text).joined(separator: ", then ")
        case "link": s += ", \(card.url?.host ?? "")"
        case "image": if card.url == nil { s += ", image in the session files" }
        default: break
        }
        return s
    }
}

extension HUDCard {
    /// Monospaced micro label at the top of every card, e.g. "STAT // SOURCE".
    var microLabel: String {
        let t = type.uppercased()
        let src = source ?? url?.host ?? ""
        return src.isEmpty ? t : "\(t) // \(src.uppercased())"
    }
}

/// Small L-shaped ticks on the four card corners.
private struct HUDCornerTicks: View {
    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            let len: CGFloat = 7
            Path { p in
                p.move(to: CGPoint(x: 0, y: len)); p.addLine(to: .zero); p.addLine(to: CGPoint(x: len, y: 0))
                p.move(to: CGPoint(x: w - len, y: 0)); p.addLine(to: CGPoint(x: w, y: 0)); p.addLine(to: CGPoint(x: w, y: len))
                p.move(to: CGPoint(x: 0, y: h - len)); p.addLine(to: CGPoint(x: 0, y: h)); p.addLine(to: CGPoint(x: len, y: h))
                p.move(to: CGPoint(x: w - len, y: h)); p.addLine(to: CGPoint(x: w, y: h)); p.addLine(to: CGPoint(x: w, y: h - len))
            }
            .stroke(hudLine.opacity(0.9), style: StrokeStyle(lineWidth: 1.5, lineCap: .square))
        }
        .allowsHitTesting(false)
    }
}

/// The card layer over the camera: up to 6 cards in edge slots around the subject.
/// `avoid` is the active shape's rect in this view's own points; slots covering it are
/// skipped, and the top 70 pt (status bar) and bottom 150 pt (controls) stay free.
struct HUDLayer: View {
    let cards: [HUDCard]
    let avoid: CGRect?

    init(cards: [HUDCard], avoid: CGRect?) {
        self.cards = cards
        self.avoid = avoid
    }

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown = false

    static func slots(in size: CGSize, avoid: CGRect?) -> [CGRect] {
        let w = size.width, h = size.height
        guard w > 60, h > 300 else { return [] }
        let top: CGFloat = 70, bottom: CGFloat = 150, margin: CGFloat = 8, gap: CGFloat = 8
        let cardW = min(w * 0.46, w - 2 * margin)
        let rowH = (h - top - bottom - 2 * gap) / 3
        let xs: [CGFloat] = [margin, w - margin - cardW]
        let ys: [CGFloat] = [top, top + rowH + gap, top + 2 * (rowH + gap)]
        var out: [CGRect] = []
        for y in ys { for x in xs { out.append(CGRect(x: x, y: y, width: cardW, height: rowH)) } }
        // TL, TR, ML, MR, BL, BR is already the loop order; drop what covers the subject.
        if let avoid = avoid, !avoid.isNull {
            out = out.filter { !$0.insetBy(dx: -8, dy: -8).intersects(avoid) }
        }
        return out
    }

    var body: some View {
        GeometryReader { geo in
            let rects = Self.slots(in: geo.size, avoid: avoid)
            ZStack(alignment: .topLeading) {
                ForEach(Array(cards.prefix(rects.count).enumerated()), id: \.offset) { i, card in
                    HUDCardView(card: card)
                        .frame(width: rects[i].width, height: rects[i].height)
                        .position(x: rects[i].midX, y: rects[i].midY)
                        .opacity(shown || reduceMotion ? 1 : 0)
                        .scaleEffect(shown || reduceMotion ? 1 : 0.92)
                }
            }
        }
        .onAppear {
            guard !reduceMotion else { shown = true; return }
            withAnimation(.easeOut(duration: 0.22)) { shown = true }  // short fade + scale
        }
    }
}
