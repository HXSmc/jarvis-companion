import Charts
import SwiftUI

/// The chart card's body (jfollow10c ruling 4): Apple's Swift Charts draws the data ChartModel
/// worked out. Labels are plain SwiftUI Text (never markdown or HTML: they may be web text).
/// No animation.
public struct ChartCardView: View {
    let card: ChartCard
    let model: ChartModel
    var scale: CGFloat = 1

    /// Series colours, led by the app accent; each has >= 3:1 contrast on the dark card.
    static let palette: [Color] = [
        Theme.accent,
        Color(red: 0.36, green: 0.68, blue: 1.0),
        Color(red: 1.0, green: 0.80, blue: 0.30),
        Color(red: 0.45, green: 0.85, blue: 0.60),
    ]

    public init?(card: ChartCard, scale: CGFloat = 1) {
        guard let model = ChartModel(card: card) else { return nil }
        self.card = card
        self.model = model
        self.scale = scale
    }

    static func color(_ i: Int) -> Color {
        palette[i % palette.count].opacity(i < palette.count ? 1 : 0.55)
    }

    private var seriesNames: [String] { card.series.map(\.name) }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            chart
                .frame(height: (model.kind == "gauge" ? 72 : 180) * scale)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(Text(verbatim: model.summary))
            legend
            if let caption = card.caption, !caption.isEmpty {
                Text(verbatim: caption)
                    .font(ScaledType.small(scale))
                    .foregroundStyle(Color.white.opacity(0.72))
            }
        }
        .transaction { $0.animation = nil }
    }

    @ViewBuilder private var chart: some View {
        switch model.kind {
        case "pie", "donut":
            Chart(Array(model.slices.enumerated()), id: \.offset) { item in
                SectorMark(angle: .value("Value", item.element.value),
                           innerRadius: .ratio(model.kind == "donut" ? 0.6 : 0),
                           angularInset: 1)
                    .foregroundStyle(Self.color(item.offset))
            }
        case "scatter":
            Chart(Array(model.points.enumerated()), id: \.offset) { item in
                PointMark(x: .value("X", item.element.x), y: .value("Y", item.element.y))
                    .foregroundStyle(Theme.accent)
            }
            .chartYScale(domain: model.yDomain)
            .modifier(Axes(scale: scale))
        case "gauge":
            Gauge(value: model.gaugeValue, in: model.gaugeRange) {
                EmptyView()
            } currentValueLabel: {
                Text(verbatim: ChartModel.number(model.gaugeValue) + (card.unit.map { " " + $0 } ?? ""))
                    .font(ScaledType.small(scale).monospacedDigit())
                    .foregroundStyle(Color.white)
            } minimumValueLabel: {
                Text(verbatim: ChartModel.number(model.gaugeRange.lowerBound))
                    .font(ScaledType.small(scale)).foregroundStyle(Color.white.opacity(0.72))
            } maximumValueLabel: {
                Text(verbatim: ChartModel.number(model.gaugeRange.upperBound))
                    .font(ScaledType.small(scale)).foregroundStyle(Color.white.opacity(0.72))
            }
            .gaugeStyle(.accessoryLinearCapacity)
            .tint(Theme.accent)
        default:
            Chart(Array(model.rows.enumerated()), id: \.offset) { item in
                let r = item.element
                switch model.kind {
                case "line":
                    LineMark(x: .value("Label", r.label), y: .value("Value", r.value))
                        .foregroundStyle(by: .value("Series", r.series))
                    PointMark(x: .value("Label", r.label), y: .value("Value", r.value))
                        .foregroundStyle(by: .value("Series", r.series))
                case "area":
                    AreaMark(x: .value("Label", r.label), y: .value("Value", r.value), stacking: .unstacked)
                        .foregroundStyle(by: .value("Series", r.series))
                        .opacity(0.7)
                case "stacked_bar":
                    BarMark(x: .value("Label", r.label), y: .value("Value", r.value), stacking: .standard)
                        .foregroundStyle(by: .value("Series", r.series))
                default:  // bar: groups side by side
                    BarMark(x: .value("Label", r.label), y: .value("Value", r.value))
                        .foregroundStyle(by: .value("Series", r.series))
                        .position(by: .value("Series", r.series))
                }
            }
            .chartForegroundStyleScale(domain: seriesNames, range: Array(Self.palette.prefix(seriesNames.count)))
            .chartLegend(.hidden)
            .chartYScale(domain: model.yDomain)
            .modifier(Axes(scale: scale))
        }
    }

    /// Pie/donut: "Rent 69%" per slice; other kinds: one swatch per series when there are two or more.
    @ViewBuilder private var legend: some View {
        let items: [(String, Color)] = model.slices.isEmpty
            ? (card.series.count > 1 ? card.series.enumerated().map { ($0.element.name, Self.color($0.offset)) } : [])
            : model.slices.enumerated().map { ("\($0.element.label) \($0.element.percent)%", Self.color($0.offset)) }
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(spacing: 6) {
                        Rectangle().fill(item.1).frame(width: 10 * scale, height: 10 * scale)
                        Text(verbatim: item.0)
                            .font(ScaledType.small(scale))
                            .foregroundStyle(Color.white.opacity(0.85))
                            .lineLimit(1)
                    }
                }
            }
        }
    }
}

/// Light axes and labels for a dark card.
private struct Axes: ViewModifier {
    let scale: CGFloat

    func body(content: Content) -> some View {
        content
            .chartXAxis {
                AxisMarks { _ in
                    AxisValueLabel().font(ScaledType.small(scale)).foregroundStyle(Color.white.opacity(0.72))
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading) { _ in
                    AxisGridLine().foregroundStyle(Color.white.opacity(0.12))
                    AxisValueLabel().font(ScaledType.small(scale)).foregroundStyle(Color.white.opacity(0.72))
                }
            }
    }
}
