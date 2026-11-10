import Foundation
import CoreGraphics
import ImageIO

// MARK: - Media policy (pure logic; no AppKit/SwiftUI/WebKit/AVKit here)

/// Pure decisions and sizes shared by the media views, loader and player. Nothing here touches
/// the network, disk or UI.
public enum MediaPolicy {
    public static let maxMediaItems = 6
    public static let maxAlt = 120

    /// ^[A-Za-z0-9_-]{11}$
    public static func isValidYouTubeID(_ id: String) -> Bool {
        id.count == 11 && id.allSatisfy { ($0.isASCII && $0.isLetter) || ($0.isASCII && $0.isNumber) || $0 == "_" || $0 == "-" }
    }

    /// https only; host match exact, case-insensitive; other query params ignored. Forms:
    /// www.youtube.com/watch?v=ID, youtube.com/watch?v=ID, m.youtube.com/watch?v=ID, youtu.be/ID,
    /// www.youtube.com/shorts/ID, www.youtube.com/embed/ID, www.youtube-nocookie.com/embed/ID.
    public static func youtubeID(fromURL string: String) -> String? {
        guard isSafeHTTPSURL(string), let comps = URLComponents(string: string),
              let host = comps.host?.lowercased() else { return nil }
        if host == "www.youtube.com" || host == "youtube.com" || host == "m.youtube.com",
           comps.path == "/watch",
           let v = comps.queryItems?.first(where: { $0.name == "v" })?.value,
           isValidYouTubeID(v) {
            return v
        }
        let parts = comps.path.components(separatedBy: "/")
        func idFromSegment(_ segment: String) -> String? { isValidYouTubeID(segment) ? segment : nil }
        if host == "youtu.be", parts.count == 2, parts[0].isEmpty { return idFromSegment(parts[1]) }
        if parts.count == 3, parts[0].isEmpty {
            if parts[1] == "shorts", host == "www.youtube.com" { return idFromSegment(parts[2]) }
            if parts[1] == "embed", host == "www.youtube.com" || host == "www.youtube-nocookie.com" {
                return idFromSegment(parts[2])
            }
        }
        return nil
    }

    public static func thumbnailURL(id: String) -> URL { URL(string: "https://i.ytimg.com/vi/\(id)/hqdefault.jpg")! }
    public static func embedURL(id: String) -> URL {
        URL(string: "https://www.youtube-nocookie.com/embed/\(id)?autoplay=1&playsinline=1&rel=0&modestbranding=1&fs=0")!
    }
    public static func watchURL(id: String) -> URL { URL(string: "https://www.youtube.com/watch?v=\(id)")! }

    /// Referer the embed request carries (YouTube RMF "API Client Identity": https + bundle id).
    public static let embedReferer = "https://com.jarvis.overlay"

    /// Main-frame navigations the player web view may take: https, host www.youtube-nocookie.com
    /// (case-insensitive), path starting "/embed/".
    public static func isEmbedNavigation(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "https",
              url.host?.lowercased() == "www.youtube-nocookie.com" else { return false }
        return url.path.hasPrefix("/embed/")
    }

    /// Path (lowercased, no query/fragment) ends in .mp4, .mov, .m4v or .m3u8.
    public static func isDirectVideoURL(_ string: String) -> Bool {
        guard let url = URL(string: string) else { return false }
        let path = url.path.lowercased()
        return [".mp4", ".mov", ".m4v", ".m3u8"].contains { path.hasSuffix($0) }
    }

    /// Content-Type header value whose media type (before ";", trimmed, lowercased) is in
    /// ImageLimits.allowedContentTypes. nil -> false.
    public static func isAllowedContentType(_ value: String?) -> Bool {
        guard let mediaType = value?.components(separatedBy: ";").first?
            .trimmingCharacters(in: .whitespaces).lowercased(), !mediaType.isEmpty else { return false }
        return ImageLimits.allowedContentTypes.contains(mediaType)
    }

    /// Pixel size WITHOUT decoding pixels: PNG IHDR bytes first (signature + "IHDR", big-endian
    /// width at 16..<20, height at 20..<24), else ImageIO properties (kCGImagePropertyPixelWidth/Height).
    public static func imagePixelDimensions(data: Data) -> (width: Int, height: Int)? {
        if data.count >= 24,
           data.prefix(8) == Data([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
           data[12..<16] == Data("IHDR".utf8) {
            let width = data[16..<20].reduce(0) { $0 << 8 | Int($1) }
            let height = data[20..<24].reduce(0) { $0 << 8 | Int($1) }
            return (width, height)
        }
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
              let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = props[kCGImagePropertyPixelWidth] as? Int,
              let height = props[kCGImagePropertyPixelHeight] as? Int else { return nil }
        return (width, height)
    }

    /// nil when: no image source, its type (CGImageSourceGetType) not in ImageLimits.allowedImageTypes,
    /// no dimensions, width*height > ImageLimits.maxSourcePixels. Else an ImageIO thumbnail.
    public static func downsample(data: Data) -> CGImage? {
        guard let dimensions = imagePixelDimensions(data: data),
              dimensions.width * dimensions.height <= ImageLimits.maxSourcePixels,
              let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
              let type = CGImageSourceGetType(source) as String?,
              ImageLimits.allowedImageTypes.contains(type) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: ImageLimits.maxDecodedSide,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    }

    /// true only for a placed, visible card that is not aside and not retired.
    public static func shouldAutoLoad(cardVisible: Bool, aside: Bool, retired: Bool) -> Bool {
        cardVisible && !aside && !retired
    }

    // Sizes (points). Media fill the card's full width under a fixed title bar.
    public static let cardWidth: CGFloat = 420
    public static let titleBarHeight: CGFloat = 48     // "chrome"
    public static let footerHeight: CGFloat = 36       // video cards: "Open in browser" row
    public static let maxCardHeight: CGFloat = 320     // single-image cards
    public static let gridGap: CGFloat = 4
    public static let videoAreaSize = CGSize(width: 420, height: 236)   // floor(420 * 9 / 16)

    /// Single image area: width 420, height = min(maxCardHeight - titleBarHeight, 420 / aspect),
    /// aspect = width/height clamped to 0.5...2.5. Zero/negative input -> videoAreaSize (placeholder).
    public static func imageAreaSize(pixelWidth: Int, pixelHeight: Int) -> CGSize {
        guard pixelWidth > 0, pixelHeight > 0 else { return videoAreaSize }
        let aspect = min(2.5, max(0.5, CGFloat(pixelWidth) / CGFloat(pixelHeight)))
        return CGSize(width: cardWidth, height: min(maxCardHeight - titleBarHeight, cardWidth / aspect))
    }

    /// 2 -> (2,1); 3...4 -> (2,2); 5...6 -> (3,2); otherwise (1,1).
    public static func galleryGrid(count: Int) -> (columns: Int, rows: Int) {
        switch count {
        case 2: return (2, 1)
        case 3...4: return (2, 2)
        case 5...6: return (3, 2)
        default: return (1, 1)
        }
    }

    /// Square tile side: (cardWidth - (columns - 1) * gridGap) / columns.
    public static func galleryTileSide(count: Int) -> CGFloat {
        let columns = galleryGrid(count: count).columns
        return (cardWidth - CGFloat(columns - 1) * gridGap) / CGFloat(columns)
    }

    /// cardWidth x (rows * side + (rows - 1) * gridGap).
    public static func galleryAreaSize(count: Int) -> CGSize {
        let grid = galleryGrid(count: count)
        let side = galleryTileSide(count: count)
        return CGSize(width: cardWidth,
                      height: CGFloat(grid.rows) * side + CGFloat(grid.rows - 1) * gridGap)
    }

    /// Whole card size for media cards, nil for other types:
    /// image with 2+ images: titleBarHeight + galleryAreaSize.height;
    /// image with 1 image: titleBarHeight + (imagePixels.map { imageAreaSize } ?? videoAreaSize).height;
    /// image with none: titleBarHeight + videoAreaSize.height;
    /// video: titleBarHeight + videoAreaSize.height + footerHeight. Width always cardWidth.
    public static func cardSize(for card: HUDCard, imagePixels: CGSize? = nil) -> CGSize? {
        switch card {
        case .image(let p):
            let areaHeight: CGFloat
            let count = p.images.count
            if count >= 2 {
                areaHeight = galleryAreaSize(count: count).height
            } else if count == 1 {
                areaHeight = (imagePixels.map { imageAreaSize(pixelWidth: Int($0.width), pixelHeight: Int($0.height)) }
                    ?? videoAreaSize).height
            } else {
                areaHeight = videoAreaSize.height
            }
            return CGSize(width: cardWidth, height: titleBarHeight + areaHeight)
        case .video:
            return CGSize(width: cardWidth, height: titleBarHeight + videoAreaSize.height + footerHeight)
        default:
            return nil
        }
    }
}

// MARK: - Host guard (SSRF refusal before any connection is made)

/// Byte-level refusal of private, loopback, link-local and multicast address ranges. The loader
/// re-checks every address a name resolves to; this gate only stops the obvious.
public enum HostGuard {
    /// 4 bytes (IPv4) or 16 bytes (IPv6); any other length -> false. Refuses IPv4 0/8, 10/8,
    /// 100.64/10, 127/8, 169.254/16, 172.16/12, 192.168/16, 224/4, 240/4 (incl. 255.255.255.255);
    /// IPv6 ::, ::1, fc00::/7, fe80::/10, ff00::/8; ::ffff:a.b.c.d and 64:ff9b::/96 are checked as
    /// their embedded IPv4.
    public static func isPublic(address: [UInt8]) -> Bool {
        switch address.count {
        case 4: return isPublicIPv4(address)
        case 16: return isPublicIPv6(address)
        default: return false
        }
    }

    /// inet_pton for an IPv4 or IPv6 literal (surrounding [ ] stripped) -> bytes, else nil.
    public static func addressBytes(_ literal: String) -> [UInt8]? {
        var text = literal
        if text.hasPrefix("[") && text.hasSuffix("]") {
            text = String(text.dropFirst().dropLast())
        }
        var addr4 = in_addr()
        if text.withCString({ inet_pton(AF_INET, $0, &addr4) }) == 1 {
            return withUnsafeBytes(of: addr4) { Array($0) }
        }
        var addr6 = in6_addr()
        if text.withCString({ inet_pton(AF_INET6, $0, &addr6) }) == 1 {
            return withUnsafeBytes(of: addr6) { Array($0) }
        }
        return nil
    }

    /// Lowercased, one trailing "." and [ ] stripped. Refuses empty, "localhost", "*.local",
    /// "*.localhost", "*.internal", "*.home.arpa", a single label (no "."), and a literal IP that
    /// fails isPublic. Everything else true (the loader still resolves and checks every address).
    public static func isAllowedHostName(_ host: String) -> Bool {
        var name = host.lowercased()
        if name.hasPrefix("[") && name.hasSuffix("]") {
            name = String(name.dropFirst().dropLast())
        }
        if name.hasSuffix(".") { name = String(name.dropLast()) }
        guard !name.isEmpty, name != "localhost",
              !name.hasSuffix(".local"), !name.hasSuffix(".localhost"),
              !name.hasSuffix(".internal"), !name.hasSuffix(".home.arpa"),
              name.contains(".") else { return false }
        if let bytes = addressBytes(name) { return isPublic(address: bytes) }
        return true
    }

    private static func isPublicIPv4(_ bytes: [UInt8]) -> Bool {
        if bytes[0] == 0 || bytes[0] == 10 || bytes[0] == 127 || bytes[0] >= 224 { return false }
        if bytes[0] == 169 && bytes[1] == 254 { return false }         // 169.254/16
        if bytes[0] == 172 && bytes[1] & 0xf0 == 16 { return false }   // 172.16/12
        if bytes[0] == 192 && bytes[1] == 168 { return false }         // 192.168/16
        if bytes[0] == 100 && bytes[1] & 0xc0 == 64 { return false }   // CGNAT 100.64/10
        return true
    }

    private static func isPublicIPv6(_ bytes: [UInt8]) -> Bool {
        // Embedded IPv4 (::ffff:0:0/96, 64:ff9b::/96) is judged as the IPv4 it carries.
        if bytes[0...9].allSatisfy({ $0 == 0 }), bytes[10] == 0xff, bytes[11] == 0xff {
            return isPublicIPv4(Array(bytes[12...15]))
        }
        if bytes[0] == 0, bytes[1] == 0x64, bytes[2] == 0xff, bytes[3] == 0x9b,
           bytes[4...11].allSatisfy({ $0 == 0 }) {
            return isPublicIPv4(Array(bytes[12...15]))
        }
        if bytes.allSatisfy({ $0 == 0 }) { return false }                        // ::
        if bytes[0...14].allSatisfy({ $0 == 0 }), bytes[15] == 1 { return false } // ::1
        if bytes[0] & 0xfe == 0xfc { return false }                              // fc00::/7
        if bytes[0] == 0xfe, bytes[1] & 0xc0 == 0x80 { return false }            // fe80::/10
        if bytes[0] == 0xff { return false }                                     // ff00::/8
        return true
    }
}

// MARK: - Image limits

/// Hard caps the image loader enforces before and after download.
public enum ImageLimits {
    public static let maxBytes = 8 * 1024 * 1024
    public static let maxSourcePixels = 50_000_000
    public static let maxDecodedSide = 1600
    public static let maxConcurrent = 3
    public static let requestTimeout: TimeInterval = 10
    public static let resourceTimeout: TimeInterval = 20
    public static let maxRedirects = 3
    public static let cacheCostLimit = 64 * 1024 * 1024
    public static let allowedContentTypes: Set<String> = [
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic",
    ]
    public static let allowedImageTypes: Set<String> = [
        "public.jpeg", "public.png", "com.compuserve.gif", "org.webmproject.webp", "public.heic",
    ]
}
