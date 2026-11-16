import Foundation

// MARK: - Chart card wire types (jfollow10c ruling 4)

/// One named series. `values` for every kind but scatter; scatter carries `points` ([x, y] pairs),
/// which travel in the same `values` key on the wire.
public struct ChartSeries: Codable, Equatable {
    public var name: String
    public var values: [Double]
    public var points: [[Double]]

    public init(name: String, values: [Double] = [], points: [[Double]] = []) {
        self.name = name
        self.values = values
        self.points = points
    }

    private enum CodingKeys: String, CodingKey { case name, values }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = (try? c.decode(String.self, forKey: .name)) ?? ""
        if let flat = try? c.decode([Double].self, forKey: .values) {
            values = flat
            points = []
        } else {
            values = []
            points = try c.decode([[Double]].self, forKey: .values)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(name, forKey: .name)
        if points.isEmpty {
            try c.encode(values, forKey: .values)
        } else {
            try c.encode(points, forKey: .values)
        }
    }
}

/// A native chart card: the model picks the kind and the numbers, the app draws it. Decoding
/// re-checks common/hud.py _chart's rules through ChartModel (second wall): a card it refuses
/// throws, so it is skipped like an unknown type.
public struct ChartCard: Codable, Equatable {
    public var id: String?
    public var title: String
    public var chart: String
    public var labels: [String]
    public var series: [ChartSeries]
    public var unit: String?
    public var caption: String?
    public var min: Double?
    public var max: Double?

    public init(id: String? = nil, title: String, chart: String, labels: [String] = [],
                series: [ChartSeries], unit: String? = nil, caption: String? = nil,
                min: Double? = nil, max: Double? = nil) {
        self.id = id
        self.title = title
        self.chart = chart
        self.labels = labels
        self.series = series
        self.unit = unit
        self.caption = caption
        self.min = min
        self.max = max
    }

    private enum CodingKeys: String, CodingKey { case id, title, chart, labels, series, unit, caption, min, max }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id)
        title = try c.decode(String.self, forKey: .title)
        chart = try c.decode(String.self, forKey: .chart)
        labels = (try c.decodeIfPresent([String].self, forKey: .labels) ?? []).map { String($0.prefix(ChartModel.maxLabel)) }
        series = try c.decode([ChartSeries].self, forKey: .series)
        unit = try c.decodeIfPresent(String.self, forKey: .unit)
        caption = try c.decodeIfPresent(String.self, forKey: .caption)
        min = try c.decodeIfPresent(Double.self, forKey: .min)
        max = try c.decodeIfPresent(Double.self, forKey: .max)
        guard ChartModel(card: self) != nil else {
            throw DecodingError.dataCorruptedError(forKey: .chart, in: c, debugDescription: "chart refused")
        }
    }
}

// MARK: - Chart model (Foundation only: CI and a Linux scratch package test every rule here)

/// What ChartCardView draws, worked out once: rows for the grid kinds, slices with percents for
/// pie/donut, points for scatter, a gauge fraction, the y-domain and a spoken-style summary.
/// `init?` returns nil for anything common/hud.py _chart would drop.
public struct ChartModel: Equatable {
    public struct Row: Equatable {
        public let index: Int
        public let label: String
        public let series: String
        public let value: Double
    }

    public struct Slice: Equatable {
        public let label: String
        public let value: Double
        public let percent: Int
    }

    public struct Point: Equatable {
        public let x: Double
        public let y: Double
    }

    public static let kinds: Set<String> = ["pie", "donut", "line", "area", "bar", "stacked_bar", "scatter", "gauge"]
    public static let maxLabel = 24

    public let kind: String
    public let rows: [Row]
    public let slices: [Slice]
    public let points: [Point]
    /// One unique key per series (marks and colours are keyed by it): an empty or repeated name
    /// becomes "Series N".
    public let seriesKeys: [String]
    public let yDomain: ClosedRange<Double>
    public let gaugeValue: Double
    public let gaugeRange: ClosedRange<Double>
    public let gaugeFraction: Double
    public let summary: String

    public init?(card: ChartCard) {
        let kind = card.chart
        guard Self.kinds.contains(kind), (1...4).contains(card.series.count),
              card.series.allSatisfy({ $0.values.allSatisfy(\.isFinite) && $0.points.allSatisfy { $0.allSatisfy(\.isFinite) } })
        else { return nil }
        let labels = card.labels
        var keys: [String] = []
        for (i, s) in card.series.enumerated() {
            var key = s.name.isEmpty || keys.contains(s.name) ? "Series \(i + 1)" : s.name
            while keys.contains(key) { key += " (\(i + 1))" }
            keys.append(key)
        }
        var rows: [Row] = []
        var slices: [Slice] = []
        var points: [Point] = []
        var gauge = 0.0
        var range = 0.0...1.0
        var summary = ""
        switch kind {
        case "pie", "donut":
            let values = card.series[0].values
            let sum = values.reduce(0, +)
            guard card.series.count == 1, (2...8).contains(labels.count), values.count == labels.count,
                  values.allSatisfy({ $0 >= 0 }), sum > 0 else { return nil }
            slices = zip(labels, values).map { Slice(label: $0, value: $1, percent: Int(($1 / sum * 100).rounded())) }
            let parts = slices.map { "\($0.label) \($0.percent) percent" }.joined(separator: ", ")
            summary = "\(kind == "pie" ? "Pie" : "Donut") chart, \(slices.count) parts: \(parts)."
        case "line", "area", "bar", "stacked_bar":
            guard (2...12).contains(labels.count), card.series.allSatisfy({ $0.values.count == labels.count }) else { return nil }
            for (s, key) in zip(card.series, keys) {
                for (i, v) in s.values.enumerated() {
                    rows.append(Row(index: i, label: labels[i], series: key, value: v))
                }
            }
            let name = ["line": "Line", "area": "Area", "bar": "Bar", "stacked_bar": "Stacked bar"][kind] ?? kind
            summary = "\(name) chart, \(labels.count) labels, \(card.series.count) series, from \(labels[0]) to \(labels[labels.count - 1])."
        case "scatter":
            let raw = card.series.flatMap(\.points)
            guard card.series.allSatisfy({ $0.values.isEmpty && (1...50).contains($0.points.count) }),
                  raw.allSatisfy({ $0.count == 2 }) else { return nil }
            points = raw.map { Point(x: $0[0], y: $0[1]) }
            summary = "Scatter chart, \(points.count) points."
        default:  // gauge
            let lo = card.min ?? 0, hi = card.max ?? 100
            guard card.series.count == 1, card.series[0].values.count == 1, lo.isFinite, hi.isFinite, lo < hi else { return nil }
            range = lo...hi
            gauge = Swift.min(Swift.max(card.series[0].values[0], lo), hi)
            summary = "Gauge, \(Self.number(gauge)) of \(Self.number(lo)) to \(Self.number(hi))."
        }
        self.kind = kind
        self.rows = rows
        self.slices = slices
        self.points = points
        self.seriesKeys = keys
        self.gaugeValue = gauge
        self.gaugeRange = range
        self.gaugeFraction = (gauge - range.lowerBound) / (range.upperBound - range.lowerBound)
        self.summary = summary
        let ys = kind == "scatter" ? points.map(\.y) : rows.map(\.value)
        var lo = ys.min() ?? 0, hi = ys.max() ?? 1
        if kind == "stacked_bar" {
            hi = Swift.max(hi, Dictionary(grouping: rows, by: \.index).values.map { $0.map(\.value).reduce(0, +) }.max() ?? hi)
        }
        if kind == "bar" || kind == "area" || kind == "stacked_bar" {
            lo = Swift.min(lo, 0)
            hi = Swift.max(hi, 0)
        }
        if lo == hi { hi = lo + 1 }
        self.yDomain = lo...hi
    }

    /// 72 -> "72", 2.5 -> "2.5": plain numbers for the summary.
    static func number(_ v: Double) -> String {
        v == v.rounded() && abs(v) < 1e15 ? String(Int(v)) : String(format: "%.2f", v)
    }
}
