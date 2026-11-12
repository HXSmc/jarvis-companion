import XCTest
@testable import OverlayCore

final class WorkspaceTests: XCTestCase {
    func testOpenPagesSchemeCheck() {
        XCTAssertTrue(Workspace.openable(URL(string: "http://example.com")!))
        XCTAssertTrue(Workspace.openable(URL(string: "https://example.com/page?q=1")!))
        XCTAssertFalse(Workspace.openable(URL(string: "file:///etc/passwd")!))
        XCTAssertFalse(Workspace.openable(URL(string: "javascript:alert(1)")!))
        XCTAssertFalse(Workspace.openable(URL(string: "x-apple.systempreferences:x")!))
        XCTAssertFalse(Workspace.openable(URL(string: "smb://host/share")!))
        // nil URL or empty host: either way not openable.
        XCTAssertFalse(URL(string: "https://").map(Workspace.openable) ?? false)
    }

    func testSnapshotBody() {
        let data = Workspace.snapshotBody(
            cards: [(card: HUDCard.info(InfoCard(id: "a", title: "First", body: ["x"])), enlarged: false),
                    (card: HUDCard.stat(StatCard(id: "b", title: "Second", value: "1", label: "l")), enlarged: true)],
            pages: [(url: "file:///tmp/x", title: "Dropped")]
                + (1...13).map { (url: "https://example.com/\($0)",
                                  title: $0 == 1 ? String(repeating: "t", count: 200) : "T\($0)") })
        let obj = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
        // Cards keep their on-screen order, each with the exact card JSON plus the enlarged flag.
        let cards = obj["cards"] as! [[String: Any]]
        XCTAssertEqual(cards.count, 2)
        let first = cards[0]["card"] as! [String: Any]
        XCTAssertEqual(first["type"] as? String, "info")
        XCTAssertEqual(first["title"] as? String, "First")
        XCTAssertEqual(cards[0]["enlarged"] as? Bool, false)
        XCTAssertEqual((cards[1]["card"] as! [String: Any])["type"] as? String, "stat")
        XCTAssertEqual(cards[1]["enlarged"] as? Bool, true)
        // Pages: file: dropped, 13 capped to 12, title capped at 120.
        let pages = obj["pages"] as! [[String: Any]]
        XCTAssertEqual(pages.count, 12)
        XCTAssertEqual(pages[0]["url"] as? String, "https://example.com/1")
        XCTAssertEqual((pages[0]["title"] as! String).count, 120)
        XCTAssertTrue(pages.allSatisfy { ($0["url"] as! String).hasPrefix("https://") })
    }
}
