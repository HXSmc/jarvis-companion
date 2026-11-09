import Foundation

/// Pure layered-DAG layout for flow cards (no SwiftUI, unit-testable).
public enum FlowLayout {
    /// Depth = longest path from a root; cycles break on DFS visiting order
    /// (a back edge counts as depth 0). Always terminates.
    public static func depths(nodes: [FlowNode], edges: [FlowEdge]) -> [String: Int] {
        var incoming: [String: [String]] = [:]
        for n in nodes { incoming[n.id] = [] }
        for e in edges where incoming[e.from] != nil && incoming[e.to] != nil {
            incoming[e.to]?.append(e.from)
        }
        var memo: [String: Int] = [:]
        var visiting: Set<String> = []
        func depth(_ id: String) -> Int {
            if let cached = memo[id] { return cached }
            guard !visiting.contains(id) else { return 0 }
            visiting.insert(id)
            var d = 0
            for src in incoming[id] ?? [] { d = max(d, depth(src) + 1) }
            visiting.remove(id)
            memo[id] = d
            return d
        }
        for n in nodes { _ = depth(n.id) }
        return memo
    }

    /// Rows = distinct depths ascending (top to bottom); nodes keep input order
    /// within a row. Grid cells guarantee no overlaps.
    public static func layout(nodes: [FlowNode], edges: [FlowEdge], size: CGSize) -> [String: CGRect] {
        guard !nodes.isEmpty, size.width > 0, size.height > 0 else { return [:] }
        let depth = depths(nodes: nodes, edges: edges)
        var byDepth: [Int: [FlowNode]] = [:]
        for n in nodes { byDepth[depth[n.id] ?? 0, default: []].append(n) }
        let rows = byDepth.keys.sorted().map { byDepth[$0]! }
        let rowCount = rows.count
        let maxPerRow = max(rows.map(\.count).max() ?? 1, 1)
        let gap: CGFloat = 8
        let rowGap: CGFloat = 22  // room for the arrow and its label between rows
        let cellH = min(max((size.height - CGFloat(rowCount - 1) * rowGap) / CGFloat(rowCount), 12), 34)
        let cellW = min(max((size.width - CGFloat(maxPerRow - 1) * gap) / CGFloat(maxPerRow), 40), 220)
        var out: [String: CGRect] = [:]
        for (rowIndex, row) in rows.enumerated() {
            let y = CGFloat(rowIndex) * (cellH + rowGap)
            let rowWidth = CGFloat(row.count) * cellW + CGFloat(row.count - 1) * gap
            let x0 = (size.width - rowWidth) / 2
            for (col, node) in row.enumerated() {
                let x = x0 + CGFloat(col) * (cellW + gap)
                out[node.id] = CGRect(x: x, y: y, width: cellW, height: cellH)
            }
        }
        return out
    }
}
