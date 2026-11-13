import Foundation

/// Saved-workspace helpers (C): which URLs may be opened from a saved snapshot, and the
/// POST /overlay/workspace/<nonce> body the app answers a workspace_save with.
public enum Workspace {
    public static let maxPages = 12

    /// Only http/https with a host opens in the browser (checked again at open time).
    public static func openable(_ url: URL) -> Bool {
        guard let host = url.host, !host.isEmpty else { return false }
        let scheme = url.scheme?.lowercased()
        return scheme == "http" || scheme == "https"
    }

    /// AX roles the page search walks into on its way to the web area (Safari: window > split
    /// group > tab group > group > group > scroll area > web area; Chrome: groups); toolbars,
    /// tab buttons and sidebars are not entered, so a window with many tabs stays cheap.
    static let pageContainers: Set<String> = ["AXWindow", "AXSplitGroup", "AXTabGroup", "AXGroup",
                                              "AXScrollArea", "AXLayoutArea"]
    static let pageSearchMaxNodes = 300

    /// A value read from AX as a URL: a URL (Safari's and Chrome's AXURL is a CFURL, not a
    /// string) or a string that parses.
    public static func url(fromAX value: Any?) -> URL? {
        if let url = value as? URL { return url }
        if let text = value as? String { return URL(string: text) }
        return nil
    }

    /// The page shown in one browser window (Mac run r2, F5: the AXDocument string check found
    /// none, so a save stored pages=0): the window's own document when it is a web URL, else the
    /// URL of the first web area under it, breadth first through container roles only.
    public static func pageURL<Node>(window: Node, role: (Node) -> String?, children: (Node) -> [Node],
                                     document: (Node) -> Any?, webURL: (Node) -> Any?) -> URL? {
        if let url = url(fromAX: document(window)), openable(url) { return url }
        var queue = children(window)
        var seen = 0
        while !queue.isEmpty, seen < pageSearchMaxNodes {
            let node = queue.removeFirst()
            seen += 1
            let r = role(node) ?? ""
            if r == "AXWebArea" {
                if let url = url(fromAX: webURL(node)), openable(url) { return url }
            } else if pageContainers.contains(r) {
                queue.append(contentsOf: children(node))
            }
        }
        return nil
    }

    public static func snapshotBody(cards: [(card: HUDCard, enlarged: Bool)],
                                    pages: [(url: String, title: String)]) -> Data {
        struct Entry: Encodable { let card: HUDCard, enlarged: Bool }
        struct Page: Encodable { let url: String, title: String }
        struct Body: Encodable { let cards: [Entry], pages: [Page] }
        let body = Body(
            cards: cards.map { Entry(card: $0.card, enlarged: $0.enlarged) },
            pages: pages
                .filter { URL(string: $0.url).map(openable) == true }
                .prefix(maxPages)
                .map { Page(url: $0.url, title: String($0.title.prefix(120))) })
        return (try? JSONEncoder().encode(body)) ?? Data("{}".utf8)
    }
}
