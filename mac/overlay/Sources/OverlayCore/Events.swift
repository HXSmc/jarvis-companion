import Foundation

/// Accessibility re-check payload sent with a shape event.
public struct AXCheck: Codable, Equatable {
    public var role: String
    public var label: String?

    public init(role: String, label: String? = nil) {
        self.role = role
        self.label = label
    }
}

/// Mode of a layout event: aside parks every card as a chip, restore puts each one back; hide
/// and show fade every card out and back (nothing is removed); the rest move all cards as one group.
public enum LayoutMode: String, Equatable, CaseIterable {
    case aside, restore, hide, show, left, right, up, down
    case topLeft = "top_left", topRight = "top_right", bottomLeft = "bottom_left", bottomRight = "bottom_right"
    case otherSide = "other_side"
}

/// One NDJSON event line from POST /overlay/utterance. Unknown or malformed
/// lines decode to .unknown; decode never throws. Wire ranges are bare
/// [lo, hi] pairs; invalid pairs (lo > hi) are dropped element-wise below.
public enum OverlayEvent: Equatable {
    case say(text: String)
    case audio(text: String, rate: Int, pcm: Data)
    case shape(HUDShape, check: AXCheck?)
    case card(HUDCard)
    /// `labels`: one short tag per range ("2/3 changed"), or empty: the app then uses "changed".
    case lines(file: String, ranges: [ClosedRange<Int>], labels: [String] = [])
    case clear
    case layout(LayoutMode)
    /// Ids of cards the service sent earlier that a newer turn replaces (string ids only, <= 64 chars, max 24).
    case dropCards(ids: [String])
    case done(metrics: [String: Double], error: String?)
    case unknown

    public static func decode(line: Data) -> OverlayEvent {
        guard let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
              let type = obj["type"] as? String
        else { return .unknown }
        switch type {
        case "say":
            guard let text = obj["text"] as? String else { return .unknown }
            return .say(text: text)
        case "audio":
            guard let text = obj["text"] as? String,
                  let b64 = obj["pcm_b64"] as? String,
                  let pcm = Data(base64Encoded: b64)
            else { return .unknown }
            let rate = (obj["rate"] as? NSNumber)?.intValue ?? 24000
            return .audio(text: text, rate: rate, pcm: pcm)
        case "shape":
            guard let shape = decodeValue(HUDShape.self, obj["shape"]) else { return .unknown }
            let check = decodeValue(AXCheck.self, obj["check"])
            return .shape(shape, check: check)
        case "card":
            guard let card = decodeValue(HUDCard.self, obj["card"]) else { return .unknown }
            return .card(card)
        case "lines":
            guard let file = obj["file"] as? String,
                  let rawRanges = obj["ranges"] as? [[Any]]
            else { return .unknown }
            // Labels pair with the wire ranges by index; a list of another length is ignored.
            let rawLabels = obj["labels"] as? [String] ?? []
            let paired = rawLabels.count == rawRanges.count
            var ranges: [ClosedRange<Int>] = []
            var labels: [String] = []
            for (index, pair) in rawRanges.enumerated() {
                guard pair.count == 2,
                      let lo = (pair[0] as? NSNumber)?.intValue,
                      let hi = (pair[1] as? NSNumber)?.intValue,
                      lo <= hi
                else { continue }
                ranges.append(lo...hi)
                if paired { labels.append(String(rawLabels[index].prefix(24))) }
            }
            return .lines(file: file, ranges: ranges, labels: labels)
        case "clear":
            return .clear
        case "layout":
            guard let raw = obj["mode"] as? String, let mode = LayoutMode(rawValue: raw) else { return .unknown }
            return .layout(mode)
        case "drop_cards":
            guard let raw = obj["ids"] as? [Any] else { return .unknown }
            let ids = raw.compactMap { $0 as? String }.filter { $0.count <= 64 }
            return .dropCards(ids: Array(ids.prefix(24)))
        case "done":
            var metrics: [String: Double] = [:]
            if let raw = obj["metrics"] as? [String: Any] {
                for (key, value) in raw {
                    if let n = value as? NSNumber { metrics[key] = n.doubleValue }
                }
            }
            let error = obj["error"] as? String
            return .done(metrics: metrics, error: error)
        default:
            return .unknown
        }
    }

    private static func decodeValue<T: Decodable>(_ type: T.Type, _ raw: Any?) -> T? {
        guard let raw = raw, JSONSerialization.isValidJSONObject(raw),
              let data = try? JSONSerialization.data(withJSONObject: raw)
        else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }
}

/// Control events from the service that steer the overlay itself rather than its content.
/// Unknown or malformed lines decode to nil; decode never throws.
public enum ControlEvent: Equatable {
    case endListening
    case cardSize(id: String, max: Bool)
    case workspaceSave(nonce: String)
    case openPages(urls: [URL])

    public static func decode(line: Data) -> ControlEvent? {
        guard let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
              let type = obj["type"] as? String
        else { return nil }
        switch type {
        case "end_listening":
            return .endListening
        case "card_size":
            // Same id rules as drop_cards: string, <= 64 chars, non-empty.
            guard let id = obj["id"] as? String, (1...64).contains(id.count),
                  let size = obj["size"] as? String
            else { return nil }
            switch size {
            case "max": return .cardSize(id: id, max: true)
            case "restore": return .cardSize(id: id, max: false)
            default: return nil
            }
        case "workspace_save":
            // The nonce names the POST /overlay/workspace/<nonce> answer slot: 16 lowercase hex.
            guard let nonce = obj["nonce"] as? String, nonce.count == 16,
                  nonce.allSatisfy { ("0"..."9").contains($0) || ("a"..."f").contains($0) }
            else { return nil }
            return .workspaceSave(nonce: nonce)
        case "open_pages":
            guard let raw = obj["urls"] as? [Any] else { return nil }
            let urls = raw.compactMap { $0 as? String }
                .compactMap { URL(string: $0) }
                .filter { Workspace.openable($0) }
            return .openPages(urls: Array(urls.prefix(Workspace.maxPages)))
        default:
            return nil
        }
    }
}

/// One NDJSON line during a turn: overlay content or a control event. "end_listening",
/// "card_size", "workspace_save" and "open_pages" lines still decode to `.unknown` in
/// `OverlayEvent` alone.
public enum TurnEvent: Equatable {
    case overlay(OverlayEvent)
    case control(ControlEvent)

    public static func decode(line: Data) -> TurnEvent {
        if let control = ControlEvent.decode(line: line) { return .control(control) }
        return .overlay(OverlayEvent.decode(line: line))
    }
}
