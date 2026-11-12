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
