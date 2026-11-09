import Foundation

/// One HUD explanation card (common/hud.py CARD). Everything here came off the wire from a
/// model, so `parse` re-validates what the Python side already checks: unknown type -> nil,
/// https-only URLs with a host, text clamped to the same limits. Defence in depth.
struct HUDCard: Identifiable, Equatable {
    var id: String
    let type: String  // "info" | "stat" | "bar" | "flow" | "link" | "image"
    let title: String

    struct BarItem: Equatable { let label: String; let value: Double }
    struct FlowNode: Equatable { let id: String; let text: String }
    struct FlowEdge: Equatable { let from: String; let to: String; let label: String }

    // Type payload; only the fields the card's type uses are set.
    var body: [String]?        // info: up to 6 lines
    var value: String?         // stat
    var statLabel: String?     // stat
    var items: [BarItem]?      // bar: up to 12
    var caption: String?       // bar
    var nodes: [FlowNode]?     // flow: up to 12
    var edges: [FlowEdge]?     // flow: up to 24
    var url: URL?              // link / image (https with a host only)
    var source: String?        // link
    var fileId: String?        // image, when there is no url

    // MARK: parsing (JSONSerialization output, all of it untrusted)

    /// One JSON object -> card, nil when invalid (same rules as common/hud.py).
    static func parse(_ raw: Any?) -> HUDCard? {
        guard let dict = raw as? [String: Any], let type = dict["type"] as? String,
              types.contains(type) else { return nil }
        let title = text(dict["title"], limit: 60)
        guard !title.isEmpty else { return nil }
        let ident = text(dict["id"], limit: 64)
        var card = HUDCard(id: ident.isEmpty ? String(UUID().uuidString.prefix(8)) : ident,
                           type: type, title: title)

        switch type {
        case "info":
            var source: [String] = []
            if let s = dict["body"] as? String {
                source = s.components(separatedBy: "\n")
            } else if let list = dict["body"] as? [Any] {
                source = list.map { text($0, limit: 200) }
            }
            let lines = source.map { text($0, limit: 200) }.filter { !$0.isEmpty }
            guard !lines.isEmpty else { return nil }
            card.body = Array(lines.prefix(6))

        case "stat":
            let value = text(dict["value"], limit: 24)
            guard !value.isEmpty else { return nil }
            card.value = value
            let label = text(dict["label"], limit: 40)
            if !label.isEmpty { card.statLabel = label }

        case "bar":
            guard let list = dict["items"] as? [Any] else { return nil }
            var items: [BarItem] = []
            for entry in list {
                guard let e = entry as? [String: Any] else { continue }
                let label = text(e["label"], limit: 40)
                guard let v = number(e["value"]), !label.isEmpty else { continue }
                items.append(BarItem(label: label, value: v))
                if items.count == 12 { break }
            }
            guard !items.isEmpty else { return nil }
            card.items = items
            let caption = text(dict["caption"], limit: 120)
            if !caption.isEmpty { card.caption = caption }

        case "flow":
            guard let list = dict["nodes"] as? [Any] else { return nil }
            var nodes: [FlowNode] = []
            var seen: Set<String> = []
            for entry in list {
                guard let n = entry as? [String: Any] else { continue }
                let nid = text(n["id"], limit: 64)
                let t = text(n["text"], limit: 60)
                guard !nid.isEmpty, !t.isEmpty, !seen.contains(nid) else { continue }
                seen.insert(nid)
                nodes.append(FlowNode(id: nid, text: t))
                if nodes.count == 12 { break }
            }
            guard !nodes.isEmpty else { return nil }
            var edges: [FlowEdge] = []
            if let elist = dict["edges"] as? [Any] {
                for entry in elist {
                    guard let e = entry as? [Any], e.count == 2 || e.count == 3 else { continue }
                    let from = text(e[0], limit: 64)
                    let to = text(e[1], limit: 64)
                    guard seen.contains(from), seen.contains(to) else { continue }
                    let label = e.count == 3 ? text(e[2], limit: 30) : ""
                    edges.append(FlowEdge(from: from, to: to, label: label))
                    if edges.count == 24 { break }
                }
            }
            card.nodes = nodes
            card.edges = edges

        case "link":
            guard let url = httpsURL(dict["url"]) else { return nil }
            card.url = url
            let source = text(dict["source"], limit: 60)
            if !source.isEmpty { card.source = source }

        case "image":
            if let url = httpsURL(dict["url"]) {
                card.url = url
            } else if let f = dict["file_id"] as? String, (1...128).contains(f.count),
                      f.allSatisfy({ fileIdChars.contains($0) }) {
                card.fileId = f  // an image with only a file_id is kept
            } else {
                return nil
            }

        default:
            return nil
        }
        return card
    }

    /// A JSON array -> valid cards only, at most 6.
    static func list(from raw: Any?) -> [HUDCard] {
        guard let arr = raw as? [Any] else { return [] }
        return Array(arr.compactMap { parse($0) }.prefix(6))
    }

    // MARK: validators (mirror common/hud.py)

    private static let types = ["info", "stat", "bar", "flow", "link", "image"]
    private static let fileIdChars = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")

    /// Untrusted value -> single-line string clamped to `limit` ("" when not text-like).
    private static func text(_ value: Any?, limit: Int) -> String {
        let raw: String
        if let s = value as? String {
            raw = s
        } else if let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() {
            raw = n.stringValue
        } else {
            return ""
        }
        var cleaned = ""
        for scalar in raw.unicodeScalars {
            cleaned.unicodeScalars.append(scalar.value < 0x20 || scalar.value == 0x7f ? " " : scalar)
        }
        return String(cleaned.trimmingCharacters(in: .whitespacesAndNewlines).prefix(limit))
    }

    private static func number(_ value: Any?) -> Double? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        let d = n.doubleValue
        return d.isFinite ? d : nil
    }

    /// https URL with a host, or nil (matches common/hud.py safe_https_url).
    private static func httpsURL(_ value: Any?) -> URL? {
        guard let s = value as? String, s.count <= 2048,
              !s.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f }),
              !s.contains(where: { $0.isWhitespace }) else { return nil }
        guard let url = URL(string: s), url.scheme?.lowercased() == "https",
              let host = url.host, !host.isEmpty else { return nil }
        return url
    }
}
