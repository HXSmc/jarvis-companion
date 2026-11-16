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
    public static let pages: [String: String] = ["map": "map.html", "drawing": "drawing.html"]
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
}
