import Foundation

/// The sandboxed web card's fixed rules (jfollow10b ruling 3). Foundation only, so CI and a Linux
/// scratch package can test every check here.
///
/// The web view loads ONLY files that ship in the app bundle, through the `jarvisweb://app/` scheme
/// handler: no file:// access and no http(s) from the page (a block-all content rule list, then this
/// table). Every page carries `csp`. Card data reaches the page as `data.json` from the handler; map
/// tiles as `tiles/z/x/y.png`, fetched natively through the overlay service (the token never enters
/// the web view).
///
/// Adding a page later (stage C charts, stage D 3D): put its files under mac/overlay/Resources/web/,
/// add one entry per file to `resources` and the page name to `pages`, and add one data validator to
/// `hud.WEB_PAGES` in common/hud.py (plus a Swift data type the page decodes).
public enum WebCardPolicy {
    public static let scheme = "jarvisweb"
    public static let host = "app"
    /// Page name (the card's `page`) -> its start file.
    public static let pages: [String: String] = ["map": "map.html", "drawing": "drawing.html", "model": "model.html"]
    /// Request path (no leading "/") -> (bundle file under Resources/web, MIME type). Exact hits only.
    public static let resources: [String: (file: String, mime: String)] = [
        "map.html": ("map.html", "text/html"),
        "map.js": ("map.js", "text/javascript"),
        "map.css": ("map.css", "text/css"),
        "vendor/leaflet/leaflet.js": ("vendor/leaflet/leaflet.js", "text/javascript"),
        "vendor/leaflet/leaflet.css": ("vendor/leaflet/leaflet.css", "text/css"),
        "vendor/leaflet/images/marker-icon.png": ("vendor/leaflet/images/marker-icon.png", "image/png"),
        "vendor/leaflet/images/marker-icon-2x.png": ("vendor/leaflet/images/marker-icon-2x.png", "image/png"),
        "vendor/leaflet/images/marker-shadow.png": ("vendor/leaflet/images/marker-shadow.png", "image/png"),
        // jfollow10c: the drawing page. drawing.svg is not a bundle file: the handler serves the
        // drawing card's sanitised SVG under this name, as an image, to the drawing page only.
        "drawing.html": ("drawing.html", "text/html"),
        "drawing.js": ("drawing.js", "text/javascript"),
        "drawing.css": ("drawing.css", "text/css"),
        "drawing.svg": ("", "image/svg+xml"),
        // jfollow10d: the 3D model page and three.js 0.187.0 (the 4 addons import the module by a
        // relative path; three.module.min.js asks for ./three.core.js, served from the .min file).
        // object.glb / preview.png are not bundle files: the handler fetches the card's own model
        // through ServiceClient (model page only).
        "model.html": ("model.html", "text/html"),
        "model.js": ("model.js", "text/javascript"),
        "model.css": ("model.css", "text/css"),
        "vendor/three/build/three.module.min.js": ("vendor/three/build/three.module.min.js", "text/javascript"),
        "vendor/three/build/three.core.js": ("vendor/three/build/three.core.min.js", "text/javascript"),
        "vendor/three/examples/jsm/loaders/GLTFLoader.js": ("vendor/three/examples/jsm/loaders/GLTFLoader.js", "text/javascript"),
        "vendor/three/examples/jsm/controls/OrbitControls.js": ("vendor/three/examples/jsm/controls/OrbitControls.js", "text/javascript"),
        "vendor/three/examples/jsm/utils/BufferGeometryUtils.js": ("vendor/three/examples/jsm/utils/BufferGeometryUtils.js", "text/javascript"),
        "vendor/three/examples/jsm/utils/SkeletonUtils.js": ("vendor/three/examples/jsm/utils/SkeletonUtils.js", "text/javascript"),
    ]
    public static let drawingImagePath = "drawing.svg"
    public static let dataPath = "data.json"
    public static let csp = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'"
    /// Block every load, then let only the app scheme through (WKContentRuleList JSON).
    public static let contentRules = #"[{"trigger":{"url-filter":".*"},"action":{"type":"block"}},{"trigger":{"url-filter":"^jarvisweb://app/"},"action":{"type":"ignore-previous-rules"}}]"#
    public static let maxZoom = 19

    /// The only main-frame URL the view may load for `page`.
    public static func startURL(page: String) -> URL? {
        guard let file = pages[page] else { return nil }
        return URL(string: "\(scheme)://\(host)/\(file)")
    }

    /// The request path without its leading "/", or nil for a wrong scheme/host or any escape
    /// (percent-escapes are refused outright, so no decoded traversal). Query and fragment are ignored.
    static func relativePath(_ url: URL) -> String? {
        guard url.scheme?.lowercased() == scheme, url.host == host,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        let path = components.percentEncodedPath
        guard path.hasPrefix("/"), !path.contains("%"), !path.contains("\\") else { return nil }
        let rel = String(path.dropFirst())
        guard !rel.split(separator: "/", omittingEmptySubsequences: false).contains(where: { $0 == ".." || $0 == "." || $0.isEmpty }) else { return nil }
        return rel
    }

    /// The table key a request names, or nil.
    public static func resourcePath(for url: URL) -> String? {
        guard let rel = relativePath(url), resources[rel] != nil else { return nil }
        return rel
    }

    public static func isData(_ url: URL) -> Bool {
        relativePath(url) == dataPath
    }

    /// (z, x, y) of `tiles/{z}/{x}/{y}.png` with 0 <= z <= 19 and 0 <= x, y < 2^z; else nil.
    public static func tilePath(for url: URL) -> (z: Int, x: Int, y: Int)? {
        guard let rel = relativePath(url) else { return nil }
        let parts = rel.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 4, parts[0] == "tiles", parts[3].hasSuffix(".png") else { return nil }
        let raw = [String(parts[1]), String(parts[2]), String(parts[3].dropLast(4))]
        guard raw.allSatisfy({ !$0.isEmpty && $0.count <= 7 && $0.allSatisfy({ $0 >= "0" && $0 <= "9" }) }) else { return nil }
        let nums = raw.compactMap { Int($0) }
        guard nums.count == 3, nums[0] <= maxZoom else { return nil }
        let size = 1 << nums[0]
        guard nums[1] < size, nums[2] < size else { return nil }
        return (nums[0], nums[1], nums[2])
    }

    // MARK: jfollow10d model page

    public static let modelGLBPath = "object.glb"
    public static let modelPNGPath = "preview.png"
    public static let maxGLBBytes = 50 * 1024 * 1024
    public static let maxPNGBytes = 5 * 1024 * 1024

    /// ^[a-z0-9][a-z0-9-]{0,47}$, like models3d.SLUG_RE.
    public static func isModelSlug(_ slug: String) -> Bool {
        let bytes = Array(slug.utf8)
        guard (1...48).contains(bytes.count) else { return false }
        func ok(_ b: UInt8, dash: Bool) -> Bool { (b >= 97 && b <= 122) || (b >= 48 && b <= 57) || (dash && b == 45) }
        return ok(bytes[0], dash: false) && bytes.dropFirst().allSatisfy { ok($0, dash: true) }
    }

    /// The overlay service path for a model card's own file, or nil for a bad slug or kind.
    public static func modelFilePath(slug: String, kind: String) -> String? {
        guard isModelSlug(slug), kind == "glb" || kind == "png" else { return nil }
        return "/overlay/model3d/\(slug)/\(kind)"
    }

    /// A GLB the page may parse: magic "glTF", version 2, header length == byte count, <= 50 MB.
    public static func acceptGLB(_ data: Data) -> Bool {
        let b = [UInt8](data.prefix(12))
        guard data.count >= 12, data.count <= maxGLBBytes, b[0] == 0x67, b[1] == 0x6C, b[2] == 0x54, b[3] == 0x46 else { return false }
        func u32(_ i: Int) -> Int { Int(b[i]) | Int(b[i + 1]) << 8 | Int(b[i + 2]) << 16 | Int(b[i + 3]) << 24 }
        return u32(4) == 2 && u32(8) == data.count
    }

    /// A PNG the page may show: the 8-byte signature and <= 5 MB.
    public static func acceptPNG(_ data: Data) -> Bool {
        data.count > 8 && data.count <= maxPNGBytes && data.prefix(8) == Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    }

    /// "glb" / "png" for the model page's `object.glb` / `preview.png` requests, else nil.
    public static func modelFileKind(for url: URL) -> String? {
        switch relativePath(url) {
        case modelGLBPath: return "glb"
        case modelPNGPath: return "png"
        default: return nil
        }
    }

    public static let modelErrors: Set<String> = ["none", "fetch", "size", "parse", "webgl"]

    /// The model page's one status ping `model-status/<error>/<refused>` (fixed word, <= 4 digits),
    /// else nil. The page has no other way to tell the app how the load went.
    public static func modelStatus(for url: URL) -> (error: String, refused: Int)? {
        guard let rel = relativePath(url) else { return nil }
        let parts = rel.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 3, parts[0] == "model-status", modelErrors.contains(parts[1]),
              (1...4).contains(parts[2].count), parts[2].allSatisfy({ $0 >= "0" && $0 <= "9" }),
              let n = Int(parts[2]) else { return nil }
        return (parts[1], n)
    }
}

/// The model page's data (jfollow10d): the vault object's slug and which view (the 3D model or the
/// ready card's preview picture). A bad slug or view throws: the card is skipped.
public struct ModelData: Codable, Equatable {
    public var slug: String
    public var view: String

    private enum CodingKeys: String, CodingKey { case slug, view }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        slug = try c.decode(String.self, forKey: .slug)
        view = try c.decode(String.self, forKey: .view)
        guard WebCardPolicy.isModelSlug(slug), view == "model" || view == "preview" else {
            throw DecodingError.dataCorruptedError(forKey: .slug, in: c, debugDescription: "bad model data")
        }
    }
}
