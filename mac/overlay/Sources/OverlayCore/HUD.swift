import Foundation

// MARK: - Shape

/// One on-screen annotation: an outline shape in screen points (top-left origin).
/// Mirrors common/hud.py validate_shape. Unknown kinds throw and are dropped
/// element-wise by HUDMessage.
public struct HUDShape: Codable, Equatable, Identifiable {
    public enum Kind: String, Codable { case rect, circle, triangle }

    public var kind: Kind
    /// [x0, y0, x1, y1] screen points, exactly 4 numbers.
    public var box: [Double]
    public var label: String?
    public var id: String?

    public init(kind: Kind, box: [Double], label: String? = nil, id: String? = nil) {
        self.kind = kind
        self.box = box
        self.label = label
        self.id = id
    }

    private enum CodingKeys: String, CodingKey { case kind, box, label, id }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(Kind.self, forKey: .kind)
        box = try c.decode([Double].self, forKey: .box)
        guard box.count == 4 else {
            throw DecodingError.dataCorruptedError(forKey: .box, in: c, debugDescription: "box must have 4 numbers")
        }
        label = try c.decodeIfPresent(String.self, forKey: .label)
        id = try c.decodeIfPresent(String.self, forKey: .id)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(kind, forKey: .kind)
        try c.encode(box, forKey: .box)
        try c.encodeIfPresent(label, forKey: .label)
        try c.encodeIfPresent(id, forKey: .id)
    }

    /// Safe CGRect (min/max, tolerates any corner order); null rect when box is unusable.
    public var rect: CGRect {
        guard box.count == 4 else { return .zero }
        let x0 = box[0], y0 = box[1], x1 = box[2], y1 = box[3]
        return CGRect(x: min(x0, x1), y: min(y0, y1), width: abs(x1 - x0), height: abs(y1 - y0))
    }
}

// MARK: - Card payloads (flat fields, same level as "type")

public struct InfoCard: Codable, Equatable {
    public var id: String?
    public var title: String
    public var body: [String]
    public init(id: String? = nil, title: String, body: [String]) {
        self.id = id; self.title = title; self.body = body
    }
}

public struct StatCard: Codable, Equatable {
    public var id: String?
    public var title: String
    public var value: String
    public var label: String
    public init(id: String? = nil, title: String, value: String, label: String) {
        self.id = id; self.title = title; self.value = value; self.label = label
    }
}

public struct BarItem: Codable, Equatable {
    public var label: String
    public var value: Double
    public init(label: String, value: Double) { self.label = label; self.value = value }
}

public struct BarCard: Codable, Equatable {
    public var id: String?
    public var title: String
    public var items: [BarItem]
    public var caption: String?
    public init(id: String? = nil, title: String, items: [BarItem], caption: String? = nil) {
        self.id = id; self.title = title; self.items = items; self.caption = caption
    }
}

public struct FlowNode: Codable, Equatable, Identifiable {
    public var id: String
    public var text: String
    public init(id: String, text: String) { self.id = id; self.text = text }
}

public struct FlowEdge: Codable, Equatable {
    public var from: String
    public var to: String
    public var label: String?

    public init(from: String, to: String, label: String? = nil) {
        self.from = from; self.to = to; self.label = label
    }

    /// Wire form is a bare JSON array [from, to] or [from, to, label].
    public init(from decoder: Decoder) throws {
        var u = try decoder.unkeyedContainer()
        from = try u.decode(String.self)
        to = try u.decode(String.self)
        if !u.isAtEnd { label = try u.decode(String.self) }
    }

    public func encode(to encoder: Encoder) throws {
        var u = encoder.unkeyedContainer()
        try u.encode(from)
        try u.encode(to)
        try u.encodeIfPresent(label)
    }
}

public struct FlowCard: Codable, Equatable {
    public var id: String?
    public var title: String
    public var nodes: [FlowNode]
    public var edges: [FlowEdge]
    public init(id: String? = nil, title: String, nodes: [FlowNode], edges: [FlowEdge] = []) {
        self.id = id; self.title = title; self.nodes = nodes; self.edges = edges
    }
}

public struct LinkCard: Codable, Equatable {
    public var id: String?
    public var title: String
    public var url: String
    public var source: String?
    public init(id: String? = nil, title: String, url: String, source: String? = nil) {
        self.id = id; self.title = title; self.url = url; self.source = source
    }
}

public struct ImageCard: Codable, Equatable {
    public var id: String?
    public var title: String
    public var url: String?
    public var fileID: String?
    private enum CodingKeys: String, CodingKey { case id, title, url, fileID = "file_id" }
    public init(id: String? = nil, title: String, url: String? = nil, fileID: String? = nil) {
        self.id = id; self.title = title; self.url = url; self.fileID = fileID
    }
}

// MARK: - Card

/// One explanation card. Mirrors common/hud.py validate_card; unknown types throw
/// and are dropped element-wise by HUDMessage.
public enum HUDCard: Codable, Equatable, Identifiable {
    case info(InfoCard)
    case stat(StatCard)
    case bar(BarCard)
    case flow(FlowCard)
    case link(LinkCard)
    case image(ImageCard)

    /// Identifiable over the payload ids (nil when absent; ForEach by index instead).
    public var id: String? {
        switch self {
        case .info(let p): return p.id
        case .stat(let p): return p.id
        case .bar(let p): return p.id
        case .flow(let p): return p.id
        case .link(let p): return p.id
        case .image(let p): return p.id
        }
    }

    /// Wire value of the "type" discriminator.
    public var typeName: String {
        switch self {
        case .info: return "info"
        case .stat: return "stat"
        case .bar: return "bar"
        case .flow: return "flow"
        case .link: return "link"
        case .image: return "image"
        }
    }

    /// Card-chrome micro-label text.
    public var typeTag: String {
        switch self {
        case .info: return "INFO"
        case .stat: return "STAT"
        case .bar: return "CHART"
        case .flow: return "FLOW"
        case .link: return "LINK"
        case .image: return "IMAGE"
        }
    }

    private enum CodingKeys: String, CodingKey { case type }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .type) {
        case "info": self = .info(try InfoCard(from: decoder))
        case "stat": self = .stat(try StatCard(from: decoder))
        case "bar": self = .bar(try BarCard(from: decoder))
        case "flow": self = .flow(try FlowCard(from: decoder))
        case "link": self = .link(try LinkCard(from: decoder))
        case "image": self = .image(try ImageCard(from: decoder))
        case let other:
            throw DecodingError.dataCorruptedError(forKey: .type, in: c, debugDescription: "unknown card type \(other)")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(typeName, forKey: .type)
        switch self {
        case .info(let p): try p.encode(to: encoder)
        case .stat(let p): try p.encode(to: encoder)
        case .bar(let p): try p.encode(to: encoder)
        case .flow(let p): try p.encode(to: encoder)
        case .link(let p): try p.encode(to: encoder)
        case .image(let p): try p.encode(to: encoder)
        }
    }
}

// MARK: - Message

/// A full HUD payload; arrays decode lossily (one bad element is skipped, like
/// common/hud.py validate_hud) and never fail the whole message.
public struct HUDMessage: Codable, Equatable {
    public var shapes: [HUDShape]
    public var cards: [HUDCard]

    public init(shapes: [HUDShape] = [], cards: [HUDCard] = []) {
        self.shapes = shapes
        self.cards = cards
    }

    private enum CodingKeys: String, CodingKey { case shapes, cards }

    /// Never throws for element problems; a non-array shapes/cards behaves like
    /// Python's validate_hud: empty collection.
    private struct Lossy<Element: Decodable>: Decodable {
        let value: Element?
        init(from decoder: Decoder) throws { value = try? Element(from: decoder) }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        shapes = ((try? c.decode([Lossy<HUDShape>].self, forKey: .shapes)) ?? []).compactMap(\.value)
        cards = ((try? c.decode([Lossy<HUDCard>].self, forKey: .cards)) ?? []).compactMap(\.value)
    }
}

// MARK: - URL check (mirror of common/hud.py safe_https_url)

/// https URLs with a host, no whitespace/control characters, reasonable length.
func isSafeHTTPSURL(_ string: String) -> Bool {
    guard !string.isEmpty, string.count <= 2048,
          !string.contains(where: { $0.isWhitespace }),
          !string.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f })
    else { return false }
    guard let url = URL(string: string),
          url.scheme?.lowercased() == "https",
          let host = url.host, !host.isEmpty
    else { return false }
    return true
}
