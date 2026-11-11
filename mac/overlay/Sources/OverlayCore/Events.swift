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
    case lines(file: String, ranges: [ClosedRange<Int>])
    case clear
    case layout(LayoutMode)
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
            var ranges: [ClosedRange<Int>] = []
            for pair in rawRanges {
                guard pair.count == 2,
                      let lo = (pair[0] as? NSNumber)?.intValue,
                      let hi = (pair[1] as? NSNumber)?.intValue,
                      lo <= hi
                else { continue }
                ranges.append(lo...hi)
            }
            return .lines(file: file, ranges: ranges)
        case "clear":
            return .clear
        case "layout":
            guard let raw = obj["mode"] as? String, let mode = LayoutMode(rawValue: raw) else { return .unknown }
            return .layout(mode)
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
