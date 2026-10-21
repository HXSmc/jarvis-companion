import XCTest
@testable import JarvisCompanion

/// Owner-note dedupe: a session note reaches the phone live (WebSocket) and again
/// in the /notify/notes catch-up; it must show once, and never as old history.
final class NotifyLogicTests: XCTestCase {

    func testLiveNoteShowsOnceAndMovesTheCursor() {
        XCTAssertTrue(NoteCursor.live(seq: 100, after: nil).show)  // first note ever
        XCTAssertEqual(NoteCursor.live(seq: 100, after: nil).cursor, 100)
        XCTAssertTrue(NoteCursor.live(seq: 101, after: 100).show)
        XCTAssertFalse(NoteCursor.live(seq: 100, after: 101).show, "already shown by the catch-up")
        XCTAssertEqual(NoteCursor.live(seq: 100, after: 101).cursor, 101, "the cursor never goes back")
        let old = NoteCursor.live(seq: nil, after: 50)  // an older gateway sends no seq
        XCTAssertTrue(old.show)
        XCTAssertEqual(old.cursor, 50)
    }

    func testCatchUpShowsOnlyNewNotesOldestFirst() {
        let notes: [[String: Any]] = [["seq": 30, "title": "c"], ["seq": 10, "title": "a"], ["seq": 20, "title": "b"]]
        let (show, cursor) = NoteCursor.catchUp(notes, after: 15)
        XCTAssertEqual(show.compactMap { $0["title"] as? String }, ["b", "c"])
        XCTAssertEqual(cursor, 30)
        // first run: the list is history, not news
        let first = NoteCursor.catchUp(notes, after: nil)
        XCTAssertTrue(first.show.isEmpty)
        XCTAssertEqual(first.cursor, 30)
        // nothing new
        XCTAssertTrue(NoteCursor.catchUp(notes, after: 30).show.isEmpty)
        XCTAssertEqual(NoteCursor.catchUp([], after: nil).cursor, 0)
    }
}
