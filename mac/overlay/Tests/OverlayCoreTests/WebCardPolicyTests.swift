import XCTest
@testable import OverlayCore

/// jfollow10b criterion 17: the web card's scheme handler checks and content rules.
final class WebCardPolicyTests: XCTestCase {
    private func url(_ s: String) -> URL { URL(string: s)! }

    func testEveryTableFileResolves() {
        for key in WebCardPolicy.resources.keys {
            XCTAssertEqual(WebCardPolicy.resourcePath(for: url("jarvisweb://app/\(key)")), key)
        }
        XCTAssertEqual(WebCardPolicy.resourcePath(for: url("jarvisweb://app/map.js?x=1#y")), "map.js")
        XCTAssertEqual(WebCardPolicy.startURL(page: "map")?.absoluteString, "jarvisweb://app/map.html")
        XCTAssertNil(WebCardPolicy.startURL(page: "chart"))
    }

    func testTraversalAndUnknownRefused() {
        for bad in ["jarvisweb://app/../map.html", "jarvisweb://app/%2e%2e/map.html", "jarvisweb://app/etc/passwd",
                    "jarvisweb://app/MAP.HTML", "jarvisweb://app/vendor/../map.js", "jarvisweb://app/nope.js",
                    "jarvisweb://app/vendor/leaflet/LICENSE", "jarvisweb://app//map.html", "jarvisweb://app/./map.js",
                    "https://app/map.html", "file:///etc/passwd", "jarvisweb://evil/map.html", "jarvisweb://app"] {
            XCTAssertNil(WebCardPolicy.resourcePath(for: url(bad)), bad)
        }
        XCTAssertNil(WebCardPolicy.resourcePath(for: URL(fileURLWithPath: "/etc/passwd")))
    }

    func testTilePaths() {
        let t = WebCardPolicy.tilePath(for: url("jarvisweb://app/tiles/3/4/5.png"))
        XCTAssertEqual(t?.z, 3); XCTAssertEqual(t?.x, 4); XCTAssertEqual(t?.y, 5)
        XCTAssertNotNil(WebCardPolicy.tilePath(for: url("jarvisweb://app/tiles/19/524287/524287.png")))
        for bad in ["tiles/20/0/0.png", "tiles/3/8/0.png", "tiles/3/0/8.png", "tiles/-1/0/0.png", "tiles/3/4/5.jpg",
                    "tiles/3/4/99999999999999999999.png", "tiles/3/4/+5.png", "tiles/3/4.png", "tiles/a/4/5.png",
                    "tiles/3/4/5.png/x", "tiles/%33/4/5.png"] {
            XCTAssertNil(WebCardPolicy.tilePath(for: url("jarvisweb://app/\(bad)")), bad)
        }
        XCTAssertNil(WebCardPolicy.tilePath(for: url("https://app/tiles/3/4/5.png")))
    }

    func testDataPath() {
        XCTAssertTrue(WebCardPolicy.isData(url("jarvisweb://app/data.json")))
        XCTAssertFalse(WebCardPolicy.isData(url("jarvisweb://app/x/data.json")))
        XCTAssertFalse(WebCardPolicy.isData(url("https://app/data.json")))
    }

    func testCSPExact() {
        XCTAssertEqual(WebCardPolicy.csp,
                       "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'")
    }

    func testContentRulesBlockAllThenAllowAppScheme() throws {
        let rules = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(WebCardPolicy.contentRules.utf8)) as? [[String: [String: String]]])
        XCTAssertEqual(rules.count, 2)
        XCTAssertEqual(rules[0]["trigger"]?["url-filter"], ".*")
        XCTAssertEqual(rules[0]["action"]?["type"], "block")
        XCTAssertEqual(rules[1]["trigger"]?["url-filter"], "^jarvisweb://app/")
        XCTAssertEqual(rules[1]["action"]?["type"], "ignore-previous-rules")
    }
}
