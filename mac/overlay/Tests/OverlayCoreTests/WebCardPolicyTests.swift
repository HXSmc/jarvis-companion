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

/// jfollow10c criterion 16: the drawing page's files, the drawing.svg image, and the same CSP.
final class WebCardDrawingPolicyTests: XCTestCase {
    private func url(_ path: String) -> URL { URL(string: "jarvisweb://app/\(path)")! }

    func testDrawingFilesResolve() {
        for path in ["drawing.html", "drawing.js", "drawing.css", "drawing.svg"] {
            XCTAssertEqual(WebCardPolicy.resourcePath(for: url(path)), path)
        }
        XCTAssertEqual(WebCardPolicy.resources["drawing.svg"]?.mime, "image/svg+xml")
        XCTAssertEqual(WebCardPolicy.resources["drawing.html"]?.mime, "text/html")
        XCTAssertEqual(WebCardPolicy.drawingImagePath, "drawing.svg")
        XCTAssertEqual(WebCardPolicy.startURL(page: "drawing")?.absoluteString, "jarvisweb://app/drawing.html")
        XCTAssertEqual(WebCardPolicy.startURL(page: "map")?.absoluteString, "jarvisweb://app/map.html")
    }

    func testDrawingEscapesRefused() {
        for path in ["../drawing.svg", "%2e%2e/drawing.html", "DRAWING.HTML", "drawing.svg/../map.js",
                     "./drawing.svg", "drawing.svg%00", "drawing.svg/"] {
            XCTAssertNil(WebCardPolicy.resourcePath(for: url(path)), path)
        }
        XCTAssertNil(WebCardPolicy.resourcePath(for: URL(string: "https://app/drawing.svg")!))
        XCTAssertNil(WebCardPolicy.resourcePath(for: URL(string: "jarvisweb://evil/drawing.svg")!))
    }

    func testCSPUnchanged() {
        XCTAssertEqual(WebCardPolicy.csp,
                       "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'")
    }
}

/// jfollow10d criterion 22: the model page's files, three.js, the card's own model path, the GLB/PNG
/// re-checks and the status ping; the CSP stays stage B's.
final class WebCardModelPolicyTests: XCTestCase {
    private func url(_ path: String) -> URL { URL(string: "jarvisweb://app/\(path)")! }

    static func glb(version: UInt32 = 2, length: UInt32? = nil, magic: String = "glTF", count: Int = 20) -> Data {
        var d = Data(magic.utf8)
        for v in [version, length ?? UInt32(count)] { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        d.append(Data(repeating: 0, count: count - 12))
        return d
    }

    func testModelFilesResolve() {
        for path in ["model.html", "model.js", "model.css", "vendor/three/build/three.module.min.js",
                     "vendor/three/examples/jsm/loaders/GLTFLoader.js", "vendor/three/examples/jsm/controls/OrbitControls.js",
                     "vendor/three/examples/jsm/utils/BufferGeometryUtils.js", "vendor/three/examples/jsm/utils/SkeletonUtils.js"] {
            XCTAssertEqual(WebCardPolicy.resourcePath(for: url(path)), path)
            XCTAssertEqual(WebCardPolicy.resources[path]?.file, path)
        }
        XCTAssertEqual(WebCardPolicy.resourcePath(for: url("vendor/three/build/three.core.js")), "vendor/three/build/three.core.js")
        XCTAssertEqual(WebCardPolicy.resources["vendor/three/build/three.core.js"]?.file, "vendor/three/build/three.core.min.js")
        XCTAssertEqual(WebCardPolicy.resources["vendor/three/build/three.core.js"]?.mime, "text/javascript")
        XCTAssertEqual(WebCardPolicy.startURL(page: "model")?.absoluteString, "jarvisweb://app/model.html")
    }

    func testModelEscapesRefused() {
        for path in ["../model.html", "%2e%2e/model.js", "vendor/three/../../model.js", "object.glb", "preview.png",
                     "vendor/three/build/three.core.min.js", "MODEL.HTML", "model.js/"] {
            XCTAssertNil(WebCardPolicy.resourcePath(for: url(path)), path)
        }
        XCTAssertEqual(WebCardPolicy.modelFileKind(for: url("object.glb?x=1")), "glb")
        XCTAssertEqual(WebCardPolicy.modelFileKind(for: url("preview.png")), "png")
        XCTAssertNil(WebCardPolicy.modelFileKind(for: url("../object.glb")))
        XCTAssertNil(WebCardPolicy.modelFileKind(for: url("x/object.glb")))
    }

    func testModelFilePath() {
        XCTAssertEqual(WebCardPolicy.modelFilePath(slug: "test-mug", kind: "glb"), "/overlay/model3d/test-mug/glb")
        XCTAssertEqual(WebCardPolicy.modelFilePath(slug: "test-mug", kind: "png"), "/overlay/model3d/test-mug/png")
        for slug in ["..", "a/b", "A", "a:b", String(repeating: "a", count: 49), "", "-a", "a b", "é"] {
            XCTAssertNil(WebCardPolicy.modelFilePath(slug: slug, kind: "glb"), slug)
        }
        XCTAssertNotNil(WebCardPolicy.modelFilePath(slug: String(repeating: "a", count: 48), kind: "glb"))
        XCTAssertNil(WebCardPolicy.modelFilePath(slug: "test-mug", kind: "py"))
    }

    func testAcceptGLBAndPNG() {
        XCTAssertTrue(WebCardPolicy.acceptGLB(Self.glb()))
        XCTAssertFalse(WebCardPolicy.acceptGLB(Self.glb(magic: "glTX")))
        XCTAssertFalse(WebCardPolicy.acceptGLB(Self.glb(version: 1)))
        XCTAssertFalse(WebCardPolicy.acceptGLB(Self.glb(length: 21)))
        XCTAssertFalse(WebCardPolicy.acceptGLB(Data("glTF".utf8)))
        let big = WebCardPolicy.maxGLBBytes + 1
        XCTAssertFalse(WebCardPolicy.acceptGLB(Self.glb(length: UInt32(big), count: big)))
        let sig = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        XCTAssertTrue(WebCardPolicy.acceptPNG(sig + Data(repeating: 1, count: 20)))
        XCTAssertFalse(WebCardPolicy.acceptPNG(Data("GIF89a-not-a-png".utf8)))
        XCTAssertFalse(WebCardPolicy.acceptPNG(sig + Data(repeating: 1, count: WebCardPolicy.maxPNGBytes)))
    }

    func testModelStatusPing() {
        XCTAssertEqual(WebCardPolicy.modelStatus(for: url("model-status/none/0"))?.error, "none")
        XCTAssertEqual(WebCardPolicy.modelStatus(for: url("model-status/parse/12"))?.refused, 12)
        for path in ["model-status/evil/0", "model-status/none/12345", "model-status/none/x", "model-status/none",
                     "model-status/none/0/x", "model-status/../0"] {
            XCTAssertNil(WebCardPolicy.modelStatus(for: url(path)), path)
        }
    }

    func testCSPByteEqualToStageB() {
        XCTAssertEqual(WebCardPolicy.csp,
                       "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'")
    }
}
