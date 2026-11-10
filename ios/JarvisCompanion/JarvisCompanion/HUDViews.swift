import SwiftUI

/// Style tokens for the HUD layer: a neutral ice-white accent (yellow on the camera
/// shapes stays the only highlight colour on screen), white text on black 0.8 panels
/// with thin 1 pt borders and small corner ticks.
private let hudIce = Color.white.opacity(0.92)  // border, ticks, bars, micro labels
private let hudBody = Color.white.opacity(0.85)

/// One rendered HUD card. `maxHeight` is its slot height: flow and bar cards drop whole
/// rows that do not fit and end with a "+N more" line instead of clipping mid-row.
struct HUDCardView: View {
    let card: HUDCard
    var maxHeight: CGFloat? = nil

    init(card: HUDCard, maxHeight: CGFloat? = nil) {
        self.card = card
        self.maxHeight = maxHeight
    }

    // Every size scales with Dynamic Type (the layer caps growth at accessibility2);
    // nothing sits below the 11 pt readable floor.
    @ScaledMetric(relativeTo: .caption2) private var microSize: CGFloat = 11  // micro label, edges
    @ScaledMetric(relativeTo: .caption2) private var rowSize: CGFloat = 11    // bar rows, hosts, "+N more"
    @ScaledMetric(relativeTo: .caption) private var nodeSize: CGFloat = 12   // flow node text
    @ScaledMetric(relativeTo: .footnote) private var bodySize: CGFloat = 13   // info lines, Open
    @ScaledMetric(relativeTo: .body) private var titleSize: CGFloat = 16      // title, photo icons
    @ScaledMetric(relativeTo: .largeTitle) private var statSize: CGFloat = 30 // stat value

    @State private var imageLoaded = false  // image cards fetch only after a tap

    var body: some View {
        if card.type == "link" {
            // VoiceOver must reach the Open link, so the card contains its children.
            chrome.accessibilityElement(children: .contain)
        } else if card.type == "image", card.url != nil, !imageLoaded {
            chrome
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(accessibilityText + ", tap to load image")
                .accessibilityAction { imageLoaded = true }
        } else {
            chrome
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(accessibilityText)
        }
    }

    private var chrome: some View {
        VStack(alignment: .leading, spacing: 8) {
            microLabelView
            Text(card.title)
                .font(.system(size: titleSize, weight: .bold))
                .fontWidth(.condensed)
                .foregroundColor(.white)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            content
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 4, style: .continuous).fill(Color.black.opacity(0.8)))
        .overlay(RoundedRectangle(cornerRadius: 4, style: .continuous).strokeBorder(hudIce.opacity(0.55), lineWidth: 1))
        .overlay(HUDCornerTicks())
        .clipShape(RoundedRectangle(cornerRadius: 4, style: .continuous))
    }

    /// "TYPE // SOURCE": the type never truncates, the source loses its head when tight.
    private var microLabelView: some View {
        HStack(spacing: 0) {
            Text(card.type.uppercased())
                .kerning(0.6)
                .fixedSize()
            if let source = microSource {
                Text(" // \(source)")
                    .kerning(0.6)
                    .lineLimit(1)
                    .truncationMode(.head)
            }
        }
        .font(.system(size: microSize, weight: .semibold, design: .monospaced))
        .foregroundColor(hudIce)
        .accessibilityHidden(true)
    }

    private var microSource: String? {
        let s = (card.source ?? card.url?.host ?? "").uppercased()
        return s.isEmpty ? nil : s
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
                    .font(.system(size: bodySize))
                    .foregroundColor(hudBody)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var statBody: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(card.value ?? "")
                .font(.system(size: statSize, weight: .heavy))
                .fontWidth(.condensed)
                .foregroundColor(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
            if let label = card.statLabel, !label.isEmpty {
                Text(label)
                    .font(.system(size: rowSize, design: .monospaced))
                    .foregroundColor(hudBody)
                    .lineLimit(2)
            }
        }
    }

    private var barBody: some View {
        let fit = barFit
        let items = Array((card.items ?? []).prefix(fit.rows))
        let peak = max(items.map(\.value).max() ?? 1, 0.001)
        return VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                // Label + value on one line, the bar under them: readable in a narrow slot too.
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(item.label)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        Text(Self.shortNumber(item.value))
                            .fixedSize()
                    }
                    .font(.system(size: rowSize, design: .monospaced))
                    .foregroundColor(hudBody)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.white.opacity(0.15))
                            Capsule().fill(hudIce)
                                .frame(width: geo.size.width * CGFloat(max(0, item.value) / peak))
                        }
                    }
                    .frame(height: 6)
                }
            }
            if fit.caption, let caption = card.caption {
                Text("sourced from \(caption)")
                    .font(.system(size: rowSize, design: .monospaced))
                    .foregroundColor(hudBody)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }
            if fit.dropped > 0 { moreLineView(fit.dropped) }
        }
    }

    private var flowBody: some View {
        let fit = flowFit
        let nodes = Array((card.nodes ?? []).prefix(fit.nodes))
        let extras = Array(extraEdges().prefix(fit.extras))
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(nodes.enumerated()), id: \.offset) { i, node in
                if i > 0 { connector(from: nodes[i - 1], to: node) }
                Text(node.text)
                    .font(.system(size: nodeSize, weight: .medium))
                    .foregroundColor(.white)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 8).padding(.vertical, 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Color.white.opacity(0.35), lineWidth: 1))
            }
            if !extras.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(extras.enumerated()), id: \.offset) { _, line in
                        Text(line)
                            .font(.system(size: rowSize, design: .monospaced))
                            .foregroundColor(hudBody)
                            .lineLimit(1)
                    }
                }
                .padding(.top, 8)
            }
            if fit.dropped > 0 { moreLineView(fit.dropped) }
        }
    }

    /// Arrow between consecutive nodes, with the edge label when there is one.
    private func connector(from a: HUDCard.FlowNode, to b: HUDCard.FlowNode) -> some View {
        let edge = card.edges?.first { $0.from == a.id && $0.to == b.id }
        return HStack(spacing: 4) {
            Image(systemName: "arrow.down")
                .font(.system(size: microSize, weight: .semibold))
                .foregroundColor(hudIce)
            if let label = edge?.label, !label.isEmpty {
                Text(label)
                    .font(.system(size: microSize, design: .monospaced))
                    .foregroundColor(hudBody)
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
                    .font(.system(size: rowSize, design: .monospaced))
                    .foregroundColor(hudBody)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            if let url = card.url {
                Link(destination: url) {
                    Text("Open")
                        .font(.system(size: bodySize, weight: .bold))
                        .foregroundColor(hudIce)
                        .padding(.horizontal, 16)
                        .frame(minHeight: 44)  // 44 pt hit area
                        .frame(maxWidth: .infinity)
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(hudIce.opacity(0.8), lineWidth: 1))
                }
            }
        }
    }

    private var imageBody: some View {
        Group {
            if let url = card.url {
                if imageLoaded {
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .success(let image):
                            image.resizable().scaledToFill()
                        default:
                            ZStack {
                                Rectangle().fill(Color.white.opacity(0.08))  // quiet placeholder
                                Image(systemName: "photo").font(.system(size: titleSize)).foregroundColor(hudBody)
                            }
                        }
                    }
                    .frame(height: 110)
                    .frame(maxWidth: .infinity)
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.2), lineWidth: 1))
                } else {
                    // Tap to load: the phone never pings a model-chosen host on its own.
                    Button {
                        imageLoaded = true
                    } label: {
                        VStack(spacing: 6) {
                            Image(systemName: "photo").font(.system(size: titleSize)).foregroundColor(hudBody)
                            Text("Tap to load image")
                                .font(.system(size: nodeSize, weight: .medium))
                                .foregroundColor(hudBody)
                            if let host = url.host {
                                Text(host)
                                    .font(.system(size: rowSize, design: .monospaced))
                                    .foregroundColor(hudBody)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                            }
                        }
                        .frame(maxWidth: .infinity)
                        .frame(height: 110)
                        .background(Color.white.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.2), lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                }
            } else {
                // file_id only: the picture is in the session files, nothing to fetch here.
                HStack(spacing: 6) {
                    Image(systemName: "photo").font(.system(size: nodeSize))
                    Text("Image in the session files").font(.system(size: nodeSize))
                }
                .foregroundColor(hudBody)
                .padding(.horizontal, 10).padding(.vertical, 8)
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.25), lineWidth: 1))
            }
        }
    }

    // MARK: row budget (flow and bar cards never clip mid-row)

    /// Leading rows of `heights` that fit `budget`, with room reserved for a "+N more"
    /// line whenever anything is dropped. At least one row always shows.
    static func fittingRows(heights: [CGFloat], budget: CGFloat?, moreHeight: CGFloat) -> Int {
        guard let budget, !heights.isEmpty, heights.reduce(0, +) > budget else { return heights.count }
        var used = moreHeight
        var shown = 0
        for h in heights {
            if used + h > budget { break }
            used += h
            shown += 1
        }
        return max(shown, 1)
    }

    /// Micro label + a 2-line title + the two 8 pt stack gaps + 12 pt padding top and bottom
    /// + the 1 pt border, paid before any content row. Estimates, deliberately generous so
    /// the border never gets cut.
    private var headerHeight: CGFloat { microSize * 1.5 + titleSize * 2.6 + 16 + 24 + 2 }

    /// Bar rows kept / dropped at the slot height (nil slot: everything), and whether the
    /// caption fits. The caption goes first, then whole rows from the end.
    var barFit: (rows: Int, dropped: Int, caption: Bool) {
        let items = card.items ?? []
        let hasCaption = !(card.caption ?? "").isEmpty
        guard let maxHeight else { return (items.count, 0, hasCaption) }
        let rowH = rowSize * 1.5 + 14  // label line + gap + 6 pt bar + row spacing
        let budget = maxHeight - headerHeight
        let captionH = rowSize * 1.4 * 2 + 7  // up to 2 lines + its top padding + row spacing
        if hasCaption, CGFloat(items.count) * rowH + captionH <= budget { return (items.count, 0, true) }
        let shown = Self.fittingRows(heights: Array(repeating: rowH, count: items.count),
                                     budget: budget, moreHeight: rowSize * 1.7)
        return (shown, items.count - shown, false)
    }

    /// Flow rows kept / dropped at the slot height: nodes first, then the "A -> B" lines.
    var flowFit: (nodes: Int, extras: Int, dropped: Int) {
        let nodes = card.nodes ?? []
        let extras = extraEdges()
        guard let maxHeight else { return (nodes.count, extras.count, 0) }
        let nodeH = nodeSize * 2.7 + 10              // a 2-line node box with its padding
        let connectorH = microSize * 1.5 + 6
        var heights = nodes.indices.map { $0 == 0 ? nodeH : nodeH + connectorH }
        heights.append(contentsOf: extras.map { _ in rowSize * 1.7 })
        let shown = Self.fittingRows(heights: heights, budget: maxHeight - headerHeight, moreHeight: rowSize * 1.7)
        return (min(shown, nodes.count), max(0, shown - nodes.count), heights.count - shown)
    }

    /// "+N more" when the slot dropped rows, nil when everything fits (unit-tested).
    var moreLineText: String? {
        let dropped = card.type == "flow" ? flowFit.dropped : barFit.dropped
        return dropped > 0 ? "+\(dropped) more" : nil
    }

    private func moreLineView(_ n: Int) -> some View {
        Text("+\(n) more")
            .font(.system(size: rowSize, design: .monospaced))
            .foregroundColor(hudBody)
            .lineLimit(1)
            .padding(.top, 4)
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
            .stroke(hudIce.opacity(0.9), style: StrokeStyle(lineWidth: 1.5, lineCap: .square))
        }
        .allowsHitTesting(false)
    }
}

/// The card layer over the camera: up to 6 cards in edge slots around the subject.
/// `avoid` is the active shape's rect in this view's own points; slots covering it are
/// trimmed or skipped, and the top 70 pt (status bar) and bottom 150 pt (controls) stay free.
/// From accessibility1 up the cards stack in one full-width column (max 3), so big text is
/// not squeezed into a half-width slot.
struct HUDLayer: View {
    let cards: [HUDCard]
    let avoid: CGRect?

    init(cards: [HUDCard], avoid: CGRect?) {
        self.cards = cards
        self.avoid = avoid
    }

    @Environment(\.dynamicTypeSize) private var typeSize

    static let top: CGFloat = 70, bottom: CGFloat = 150, margin: CGFloat = 8, gap: CGFloat = 8

    /// Grid cells, [row][column]: 3 rows of 2 half-width columns (or 1 full-width column).
    static func cells(in size: CGSize, singleColumn: Bool = false) -> [[CGRect]] {
        let w = size.width, h = size.height
        guard w > 60, h > 300 else { return [] }
        let cardW = singleColumn ? w - 2 * margin : min(w * 0.46, w - 2 * margin)
        let rowH = (h - top - bottom - 2 * gap) / 3
        let xs: [CGFloat] = singleColumn ? [margin] : [margin, w - margin - cardW]
        return (0..<3).map { r in
            xs.map { CGRect(x: $0, y: top + CGFloat(r) * (rowH + gap), width: cardW, height: rowH) }
        }
    }

    /// Every cell in fill order (TL, TR, ML, MR, BL, BR), trimmed around the subject.
    static func slots(in size: CGSize, avoid: CGRect?) -> [CGRect] {
        cells(in: size).flatMap { $0 }.compactMap { trim($0, avoid: avoid) }
    }

    /// A slot over the subject keeps its largest readable part beside, above or below it
    /// (>= 110 pt wide or >= 90 pt tall); nil when no part is big enough.
    static func trim(_ slot: CGRect, avoid: CGRect?) -> CGRect? {
        guard let avoid = avoid, !avoid.isNull else { return slot }
        let clear = avoid.insetBy(dx: -gap, dy: -gap)
        guard slot.intersects(clear) else { return slot }
        // Sizes are checked as plain numbers: CGRect.width/height report negative sizes as positive.
        var options: [CGRect] = []
        let leftW = clear.minX - slot.minX - 0.5, rightW = slot.maxX - clear.maxX - 0.5
        if slot.minX < clear.minX, leftW >= 110 {
            options.append(CGRect(x: slot.minX, y: slot.minY, width: leftW, height: slot.height))
        }
        if slot.maxX > clear.maxX, rightW >= 110 {
            options.append(CGRect(x: clear.maxX + 0.5, y: slot.minY, width: rightW, height: slot.height))
        }
        let aboveH = clear.minY - slot.minY - 0.5, belowH = slot.maxY - clear.maxY - 0.5
        if aboveH >= 90 { options.append(CGRect(x: slot.minX, y: slot.minY, width: slot.width, height: aboveH)) }
        if belowH >= 90 { options.append(CGRect(x: slot.minX, y: clear.maxY + 0.5, width: slot.width, height: belowH)) }
        return options.max { $0.width * $0.height < $1.width * $1.height }
    }

    /// A flow or bar card with more than 2 rows needs a double-height slot to explain anything.
    static func isTall(_ card: HUDCard) -> Bool {
        (card.type == "flow" && (card.nodes?.count ?? 0) > 2) || (card.type == "bar" && (card.items?.count ?? 0) > 2)
    }

    /// One rect per card shown, in card order (cards that find no slot are not shown).
    /// A tall card also takes the free cell below it in its column.
    static func placements(for cards: [HUDCard], in size: CGSize, avoid: CGRect?,
                           singleColumn: Bool = false) -> [CGRect] {
        let grid = cells(in: size, singleColumn: singleColumn)
        let cols = grid.first?.count ?? 0
        var used = Set<Int>()  // row * cols + column
        var out: [CGRect] = []
        for card in cards {
            var placed = false
            while !placed {
                guard let next = (0..<grid.count * cols).first(where: { !used.contains($0) }) else { return out }
                used.insert(next)
                let r = next / cols, c = next % cols
                if isTall(card), r + 1 < grid.count, !used.contains(next + cols),
                   let tall = trim(grid[r][c].union(grid[r + 1][c]), avoid: avoid) {
                    used.insert(next + cols)
                    out.append(tall)
                    placed = true
                } else if let rect = trim(grid[r][c], avoid: avoid) {
                    out.append(rect)
                    placed = true
                }
            }
        }
        return out
    }

    var body: some View {
        GeometryReader { geo in
            let rects = Self.placements(for: cards, in: geo.size, avoid: avoid,
                                        singleColumn: typeSize >= .accessibility1)
            ZStack(alignment: .topLeading) {
                ForEach(Array(zip(cards, rects).enumerated()), id: \.offset) { i, pair in
                    // The card budgets its rows against the slot, so nothing is clipped
                    // mid-row and the border stays whole.
                    HUDCardView(card: pair.0, maxHeight: pair.1.height)
                        .frame(width: pair.1.width)
                        .frame(height: pair.1.height, alignment: .top)
                        .position(x: pair.1.midX, y: pair.1.midY)
                        .modifier(HUDAppear(index: i))
                        .id("\(i)-\(pair.0.id)")  // a new card in this slot fades in again
                }
            }
        }
        // Cards stay usable at giant sizes: growth stops at accessibility2.
        .dynamicTypeSize(...DynamicTypeSize.accessibility2)
    }
}

/// Each card fades in on its own appear, so cards that arrive after the layer is on
/// screen (every real reply) show up too. Cards come 40 ms apart; each fade is <= 250 ms.
private struct HUDAppear: ViewModifier {
    let index: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var visible = false

    func body(content: Content) -> some View {
        content
            .opacity(visible || reduceMotion ? 1 : 0)
            .scaleEffect(visible || reduceMotion ? 1 : 0.92)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeOut(duration: 0.22).delay(Double(index) * 0.04)) { visible = true }
            }
    }
}
