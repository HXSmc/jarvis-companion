import XCTest
@testable import OverlayCore

/// jfollow10c criterion 23: the chart card's model (Foundation only; also run in a Linux scratch package).
final class ChartModelTests: XCTestCase {
    private func card(_ kind: String, labels: [String] = ["A", "B", "C"], series: [ChartSeries],
                      min: Double? = nil, max: Double? = nil) -> ChartCard {
        ChartCard(title: "T", chart: kind, labels: labels, series: series, min: min, max: max)
    }

    private let spend = [ChartSeries(name: "Spend", values: [1200, 400, 150])]
    private let labels = ["Rent", "Food", "Travel"]

    func testPieAndDonutPercents() throws {
        for kind in ["pie", "donut"] {
            let m = try XCTUnwrap(ChartModel(card: card(kind, labels: labels, series: spend)))
            XCTAssertEqual(m.slices.map(\.percent), [69, 23, 9])
            XCTAssertLessThanOrEqual(abs(m.slices.map(\.percent).reduce(0, +) - 100), 1)
        }
        let pie = try XCTUnwrap(ChartModel(card: card("pie", labels: labels, series: spend)))
        XCTAssertEqual(pie.summary, "Pie chart, 3 parts: Rent 69 percent, Food 23 percent, Travel 9 percent.")
    }

    func testGridKindsRowsAndDomain() throws {
        let two = [ChartSeries(name: "N", values: [5, 8, 3]), ChartSeries(name: "S", values: [2, 4, 6])]
        for kind in ["line", "area", "bar", "stacked_bar"] {
            let m = try XCTUnwrap(ChartModel(card: card(kind, series: two)))
            XCTAssertEqual(m.rows.count, 3 * 2, kind)
            XCTAssertEqual(m.rows.first, ChartModel.Row(index: 0, label: "A", series: "N", value: 5))
        }
        XCTAssertTrue(try XCTUnwrap(ChartModel(card: card("bar", series: two))).yDomain.contains(0))
        XCTAssertTrue(try XCTUnwrap(ChartModel(card: card("area", series: two))).yDomain.contains(0))
        XCTAssertEqual(try XCTUnwrap(ChartModel(card: card("stacked_bar", series: two))).yDomain.upperBound, 12)
        XCTAssertEqual(try XCTUnwrap(ChartModel(card: card("line", series: two))).yDomain, 2...8)
    }

    func testScatterKeepsPairs() throws {
        let pts = [ChartSeries(name: "P", points: [[160, 55], [172, 70]])]
        let m = try XCTUnwrap(ChartModel(card: card("scatter", labels: [], series: pts)))
        XCTAssertEqual(m.points, [ChartModel.Point(x: 160, y: 55), ChartModel.Point(x: 172, y: 70)])
        XCTAssertNil(ChartModel(card: card("scatter", labels: [], series: [ChartSeries(name: "P", points: [[1]])])))
    }

    func testGaugeFraction() throws {
        let g = { (v: Double) in ChartModel(card: self.card("gauge", labels: [], series: [ChartSeries(name: "C", values: [v])], min: 0, max: 100)) }
        XCTAssertEqual(try XCTUnwrap(g(75)).gaugeFraction, 0.75, accuracy: 1e-9)
        XCTAssertEqual(try XCTUnwrap(g(150)).gaugeFraction, 1.0, accuracy: 1e-9)
        XCTAssertNil(ChartModel(card: card("gauge", labels: [], series: [ChartSeries(name: "C", values: [5])], min: 10, max: 10)))
    }

    func testRefusals() {
        XCTAssertNil(ChartModel(card: card("line", series: [ChartSeries(name: "S", values: [1, .nan, 3])])))
        XCTAssertNil(ChartModel(card: card("line", series: [ChartSeries(name: "S", values: [1, 2])])))
        XCTAssertNil(ChartModel(card: card("pie", labels: labels, series: [ChartSeries(name: "S", values: [1, -1, 3])])))
        XCTAssertNil(ChartModel(card: card("pie", labels: ["A"], series: [ChartSeries(name: "S", values: [1])])))
        XCTAssertNil(ChartModel(card: card("radar", series: spend)))
        XCTAssertNil(ChartModel(card: card("bar", series: Array(repeating: ChartSeries(name: "S", values: [1, 2, 3]), count: 5))))
    }

    func testSeriesKeysAreUnique() throws {
        let series = ["", "A", "A", "Series 4"].map { ChartSeries(name: $0, values: [1, 2, 3]) }
        let m = try XCTUnwrap(ChartModel(card: card("line", series: series)))
        XCTAssertEqual(m.seriesKeys, ["Series 1", "A", "Series 3", "Series 4"])
        XCTAssertEqual(Set(m.rows.map(\.series)), Set(m.seriesKeys))
        let clash = ["Series 2", ""].map { ChartSeries(name: $0, values: [1, 2, 3]) }
        XCTAssertEqual(try XCTUnwrap(ChartModel(card: card("bar", series: clash))).seriesKeys, ["Series 2", "Series 2 (2)"])
    }
}
