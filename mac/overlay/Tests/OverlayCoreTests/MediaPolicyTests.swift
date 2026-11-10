import XCTest
import CoreGraphics
import ImageIO
@testable import OverlayCore

final class MediaPolicyTests: XCTestCase {
    // MARK: YouTube URL forms

    func testYouTubeIDAcceptsTheSevenForms() {
        let id = "dQw4w9WgXcQ"
        let accept = [
            "https://www.youtube.com/watch?v=\(id)",
            "https://youtube.com/watch?v=\(id)",
            "https://m.youtube.com/watch?v=\(id)&t=30",
            "https://youtu.be/\(id)",
            "https://www.youtube.com/shorts/\(id)",
            "https://www.youtube.com/embed/\(id)",
            "https://www.youtube-nocookie.com/embed/\(id)",
        ]
        for url in accept {
            XCTAssertEqual(MediaPolicy.youtubeID(fromURL: url), id, url)
        }
    }

    func testYouTubeIDRefusesOtherForms() {
        let refuse = [
            "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=short",
            "http://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQx",
            "https://youtu.be/dQw4w9/gXcQ",
            "https://www.youtube-nocookie.com/shorts/dQw4w9WgXcQ",  // same rule as common/hud.py
        ]
        for url in refuse {
            XCTAssertNil(MediaPolicy.youtubeID(fromURL: url), url)
        }
    }

    func testDerivedYouTubeURLs() {
        XCTAssertEqual(MediaPolicy.thumbnailURL(id: "dQw4w9WgXcQ").absoluteString,
                       "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")
        XCTAssertEqual(MediaPolicy.embedURL(id: "dQw4w9WgXcQ").absoluteString,
                       "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&playsinline=1&rel=0&modestbranding=1&fs=0")
    }

    func testIsEmbedNavigation() {
        XCTAssertTrue(MediaPolicy.isEmbedNavigation(MediaPolicy.embedURL(id: "dQw4w9WgXcQ")))
        XCTAssertFalse(MediaPolicy.isEmbedNavigation(URL(string: "https://www.youtube.com/watch?v=x")!))
        XCTAssertFalse(MediaPolicy.isEmbedNavigation(URL(string: "https://www.youtube-nocookie.com/")!))
        XCTAssertFalse(MediaPolicy.isEmbedNavigation(URL(string: "http://www.youtube-nocookie.com/embed/x")!))
    }

    // MARK: Host guard

    func testIsPublicRefusesPrivateRanges() {
        let refused = [dotted(10, 0, 0, 1), dotted(172, 16, 5, 4), dotted(172, 31, 255, 255), dotted(192, 168, 0, 1), "127.0.0.1",
                       "169.254.169.254", dotted(100, 64, 0, 1), "0.0.0.0", "224.0.0.1", "255.255.255.255",
                       "::1", "::", "fe80::1", "fd00::1", "ff02::1", "::ffff:127.0.0.1",
                       "::ffff:" + dotted(10, 0, 0, 1), "64:ff9b::a00:1"]
        for literal in refused {
            guard let bytes = HostGuard.addressBytes(literal) else { return XCTFail("no bytes for \(literal)") }
            XCTAssertFalse(HostGuard.isPublic(address: bytes), literal)
        }
    }

    func testIsPublicAllowsPublicAddresses() {
        let allowed = ["172.32.0.1", "8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8"]
        for literal in allowed {
            guard let bytes = HostGuard.addressBytes(literal) else { return XCTFail("no bytes for \(literal)") }
            XCTAssertTrue(HostGuard.isPublic(address: bytes), literal)
        }
        XCTAssertFalse(HostGuard.isPublic(address: [1, 2, 3]))
    }

    func testIsAllowedHostName() {
        let refused = ["localhost", "LOCALHOST.", "printer.local", "foo.localhost", "svc.internal",
                       "router.home.arpa", "intranet", "127.0.0.1", "[::1]", dotted(10, 1, 2, 3), ""]
        for host in refused {
            XCTAssertFalse(HostGuard.isAllowedHostName(host), host)
        }
        for host in ["upload.wikimedia.org", "i.ytimg.com", "8.8.8.8"] {
            XCTAssertTrue(HostGuard.isAllowedHostName(host), host)
        }
    }

    // MARK: Direct video and redirects

    func testDirectVideoURLRefusesHLS() {
        XCTAssertTrue(MediaPolicy.isDirectVideoURL("https://cdn.example.com/a.MP4?x=1"))
        XCTAssertTrue(MediaPolicy.isDirectVideoURL("https://cdn.example.com/a.mov"))
        XCTAssertFalse(MediaPolicy.isDirectVideoURL("https://cdn.example.com/live.m3u8"))
        XCTAssertFalse(MediaPolicy.isDirectVideoURL("https://example.com/a.html"))
    }

    func testRedirectHostDecision() {
        let ok = URL(string: "https://upload.wikimedia.org/x.jpg")
        XCTAssertEqual(MediaPolicy.redirectHost(ok, hop: 1), "upload.wikimedia.org")
        XCTAssertEqual(MediaPolicy.redirectHost(ok, hop: ImageLimits.maxRedirects), "upload.wikimedia.org")
        XCTAssertNil(MediaPolicy.redirectHost(ok, hop: ImageLimits.maxRedirects + 1))
        XCTAssertNil(MediaPolicy.redirectHost(nil, hop: 1))
        XCTAssertNil(MediaPolicy.redirectHost(URL(string: "http://upload.wikimedia.org/x.jpg"), hop: 1))
        XCTAssertNil(MediaPolicy.redirectHost(URL(string: "https://localhost/x.jpg"), hop: 1))
        XCTAssertNil(MediaPolicy.redirectHost(URL(string: "https://router.local/x.jpg"), hop: 1))
        XCTAssertNil(MediaPolicy.redirectHost(URL(string: "https://127.0.0.1/x.jpg"), hop: 1))
        XCTAssertNil(MediaPolicy.redirectHost(URL(string: "https://[::1]/x.jpg"), hop: 1))
    }

    // MARK: Image bytes

    func testSniffImageTypeFromMagicBytes() {
        XCTAssertEqual(MediaPolicy.sniffImageType(Data([0xff, 0xd8, 0xff, 0xe0, 0, 0])), "public.jpeg")
        XCTAssertEqual(MediaPolicy.sniffImageType(Data([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0])), "public.png")
        XCTAssertEqual(MediaPolicy.sniffImageType(Data("GIF89a....".utf8)), "com.compuserve.gif")
        XCTAssertEqual(MediaPolicy.sniffImageType(Data("RIFF\0\0\0\0WEBPVP8 ".utf8)), "org.webmproject.webp")
        XCTAssertEqual(MediaPolicy.sniffImageType(Data([0, 0, 0, 24] + Array("ftypheic".utf8))), "public.heic")
        // Formats ImageIO could parse but we never hand it: TIFF/DNG, OpenEXR, SVG, BMP, an MP4 box.
        XCTAssertNil(MediaPolicy.sniffImageType(Data([0x49, 0x49, 0x2a, 0x00, 8, 0, 0, 0])))
        XCTAssertNil(MediaPolicy.sniffImageType(Data([0x76, 0x2f, 0x31, 0x01, 2, 0, 0, 0])))
        XCTAssertNil(MediaPolicy.sniffImageType(Data("<svg xmlns=".utf8)))
        XCTAssertNil(MediaPolicy.sniffImageType(Data("BM".utf8)))
        XCTAssertNil(MediaPolicy.sniffImageType(Data([0, 0, 0, 24] + Array("ftypisom".utf8))))
        XCTAssertNil(MediaPolicy.sniffImageType(Data()))
        XCTAssertNil(MediaPolicy.imagePixelDimensions(data: Data([0x49, 0x49, 0x2a, 0x00, 8, 0, 0, 0])))
    }

    func testDownsampleCapsDecodedSide() throws {
        let png = try Self.makePNG(width: 4000, height: 3000)
        XCTAssertEqual(MediaPolicy.imagePixelDimensions(data: png)?.width, 4000)
        XCTAssertEqual(MediaPolicy.imagePixelDimensions(data: png)?.height, 3000)
        let image = try XCTUnwrap(MediaPolicy.downsample(data: png), "generated PNG must downsample")
        XCTAssertLessThanOrEqual(max(image.width, image.height), ImageLimits.maxDecodedSide)
        XCTAssertNil(MediaPolicy.downsample(data: Data("not an image".utf8)))
    }

    func testHugePNGHeaderIsRefused() {
        var png = Data([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])  // signature
        png.append(contentsOf: [0, 0, 0, 13])                              // chunk length
        png.append(Data("IHDR".utf8))
        png.append(contentsOf: [0x00, 0x00, 0x1f, 0x40])                   // width 8000 big-endian
        png.append(contentsOf: [0x00, 0x00, 0x1f, 0x40])                   // height 8000 big-endian
        png.append(contentsOf: [8, 6, 0, 0, 0])                            // bit depth 8, RGBA
        png.append(contentsOf: [0, 0, 0, 0])                               // CRC (never read)
        XCTAssertEqual(MediaPolicy.imagePixelDimensions(data: png)?.width, 8000)
        XCTAssertEqual(MediaPolicy.imagePixelDimensions(data: png)?.height, 8000)
        XCTAssertGreaterThan(8000 * 8000, ImageLimits.maxSourcePixels)
        XCTAssertNil(MediaPolicy.downsample(data: png))
    }

    private static func makePNG(width: Int, height: Int) throws -> Data {
        let ctx = try XCTUnwrap(CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                          bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        ctx.setFillColor(CGColor(red: 0.2, green: 0.4, blue: 0.6, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let image = try XCTUnwrap(ctx.makeImage())
        let out = NSMutableData()
        let dest = try XCTUnwrap(CGImageDestinationCreateWithData(out, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(dest, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(dest))
        return out as Data
    }

    // MARK: Limits and content types

    func testImageLimitsAndContentTypes() {
        XCTAssertEqual(ImageLimits.maxBytes, 8_388_608)
        XCTAssertEqual(ImageLimits.maxConcurrent, 3)
        XCTAssertTrue(MediaPolicy.isAllowedContentType("image/jpeg; charset=x"))
        XCTAssertTrue(MediaPolicy.isAllowedContentType("IMAGE/PNG"))
        XCTAssertFalse(MediaPolicy.isAllowedContentType("text/html"))
        XCTAssertFalse(MediaPolicy.isAllowedContentType("image/svg+xml"))
        XCTAssertFalse(MediaPolicy.isAllowedContentType(nil))
        XCTAssertFalse(MediaPolicy.isAllowedContentType(""))
    }

    // MARK: Sizes

    func testMediaSizes() {
        XCTAssertEqual(MediaPolicy.imageAreaSize(pixelWidth: 1600, pixelHeight: 400),
                       CGSize(width: 420, height: 168))
        XCTAssertEqual(MediaPolicy.imageAreaSize(pixelWidth: 400, pixelHeight: 1600).height, 272)
        let single = HUDCard.image(ImageCard(id: "s", title: "Single", url: "https://example.com/a.jpg"))
        XCTAssertEqual(MediaPolicy.cardSize(for: single, imagePixels: CGSize(width: 1600, height: 400)),
                       CGSize(width: MediaPolicy.cardWidth, height: MediaPolicy.titleBarHeight + 168))
        // Every row is full (no hole) and the grid stays within the 320 pt card.
        XCTAssertEqual(MediaPolicy.galleryRows(count: 2), [2])
        XCTAssertEqual(MediaPolicy.galleryRows(count: 3), [3])
        XCTAssertEqual(MediaPolicy.galleryRows(count: 4), [2, 2])
        XCTAssertEqual(MediaPolicy.galleryRows(count: 5), [2, 3])
        XCTAssertEqual(MediaPolicy.galleryRows(count: 6), [3, 3])
        XCTAssertEqual(MediaPolicy.galleryRows(count: 7), [])
        for count in 2...6 {
            XCTAssertEqual(MediaPolicy.galleryRows(count: count).reduce(0, +), count)
            XCTAssertLessThanOrEqual(MediaPolicy.galleryAreaSize(count: count).height,
                                     MediaPolicy.maxCardHeight - MediaPolicy.titleBarHeight)
        }
        XCTAssertEqual(MediaPolicy.galleryTileWidth(columns: 2), 208)
        XCTAssertEqual(MediaPolicy.galleryRowHeight(count: 2), 208)   // one row: square tiles
        XCTAssertEqual(MediaPolicy.galleryRowHeight(count: 3), MediaPolicy.galleryTileWidth(columns: 3))
        XCTAssertEqual(MediaPolicy.galleryRowHeight(count: 5), 134)   // two rows: (272 - 4) / 2
        XCTAssertEqual(MediaPolicy.galleryAreaSize(count: 5), CGSize(width: 420, height: 272))
        XCTAssertEqual(MediaPolicy.videoAreaSize, CGSize(width: 420, height: 236))
        let video = HUDCard.video(VideoCard(id: "v", title: "Video", youtubeID: "aqz-KE-bpKQ"))
        XCTAssertEqual(MediaPolicy.cardSize(for: video),
                       CGSize(width: MediaPolicy.cardWidth,
                              height: MediaPolicy.titleBarHeight + MediaPolicy.videoAreaSize.height + MediaPolicy.footerHeight))
        XCTAssertNil(MediaPolicy.cardSize(for: .info(InfoCard(title: "No size", body: []))))
    }

    // MARK: Debug commands and demo cards

    func testDebugCommandParse() {
        XCTAssertEqual(LayoutDebugCommand.parse("demo:media"), .demoMedia)
        XCTAssertEqual(LayoutDebugCommand.parse("demo:media-bad"), .demoMediaBad)
        XCTAssertNil(LayoutDebugCommand.parse("demo:media "))
        XCTAssertNil(LayoutDebugCommand.parse("demo:MEDIA"))
        XCTAssertNil(LayoutDebugCommand.parse("demo:media;clear"))
        XCTAssertNil(LayoutDebugCommand.parse("demo:mediax"))
        XCTAssertEqual(LayoutDebugCommand.parse("demo:3"), .demo(3))
    }

    func testDemoMediaCardsPassURLGuards() {
        let cards = LayoutDebugCommand.demoMediaCards()
        XCTAssertEqual(cards.compactMap(\.id), ["media-1", "media-2", "media-3", "media-4"])
        for url in Self.mediaURLStrings(in: cards) {
            XCTAssertTrue(isSafeHTTPSURL(url), url)
            guard let host = URL(string: url)?.host else { return XCTFail("no host in \(url)") }
            XCTAssertTrue(HostGuard.isAllowedHostName(host), url)
        }
    }

    func testDemoMediaBadCardIDs() {
        XCTAssertEqual(LayoutDebugCommand.demoMediaBadCards().compactMap(\.id),
                       ["media-bad-1", "media-bad-2", "media-bad-3"])
    }

    private static func mediaURLStrings(in cards: [HUDCard]) -> [String] {
        var urls: [String] = []
        for card in cards {
            switch card {
            case .image(let p):
                urls.append(contentsOf: [p.url, p.sourcePage].compactMap { $0 })
                for item in p.images {
                    urls.append(contentsOf: [item.url, item.sourcePage].compactMap { $0 })
                }
            case .video(let p):
                urls.append(contentsOf: [p.url, p.thumbnailURL, p.sourcePage].compactMap { $0 })
            default:
                break
            }
        }
        return urls
    }

    // MARK: Auto-load and idle

    func testShouldAutoLoad() {
        XCTAssertTrue(MediaPolicy.shouldAutoLoad(cardVisible: true, aside: false, retired: false))
        XCTAssertFalse(MediaPolicy.shouldAutoLoad(cardVisible: false, aside: false, retired: false))
        XCTAssertFalse(MediaPolicy.shouldAutoLoad(cardVisible: true, aside: true, retired: false))
        XCTAssertFalse(MediaPolicy.shouldAutoLoad(cardVisible: true, aside: false, retired: true))
    }

    func testIdleWaits() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertTrue(ScatterLayout.idleWaits(pointerOnPanel: false,
                                              playingSince: now.addingTimeInterval(-9 * 60), now: now))
        XCTAssertFalse(ScatterLayout.idleWaits(pointerOnPanel: false,
                                               playingSince: now.addingTimeInterval(-11 * 60), now: now))
        XCTAssertTrue(ScatterLayout.idleWaits(pointerOnPanel: true, playingSince: nil, now: now))
        XCTAssertFalse(ScatterLayout.idleWaits(pointerOnPanel: false, playingSince: nil, now: now))
    }
}

/// Builds a dotted-quad from octets (keeps private-range literals out of the source text).
private func dotted(_ octets: Int...) -> String {
    octets.map(String.init).joined(separator: ".")
}
