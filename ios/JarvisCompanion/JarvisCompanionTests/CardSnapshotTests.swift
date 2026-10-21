import SwiftUI
import UIKit
import XCTest
@testable import JarvisCompanion

/// Session cards (gateway/cards.py): the gateway JSON decodes, each ask gets the
/// right buttons, and every card state is drawn in a real simulator window to
/// PNG ($CARD_SHOTS_DIR, set by CI through TEST_RUNNER_CARD_SHOTS_DIR) for the
/// owner's before/after page. No network: files are preloaded.
@MainActor
final class CardSnapshotTests: XCTestCase {

    // Same shape gateway/central.py sends (snake_case, `card` on every row).
    static func session(_ json: String) throws -> CentralSession {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(CentralSession.self, from: Data(json.utf8))
    }

    static let now = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-20))

    static let before = """
    {"session_id": "b3058c67-a5c3", "title": "New project task from the owner: Jarvis", "state": "idle",
     "phase": "done", "health": "done", "device": "Mini PC", "project": "jarvis-companion",
     "doing": "New project task from the owner: Jarvis",
     "recap": ["I fixed the page header", "I am now updating the final report with the first steps for the owner"],
     "last_activity_at": "\(now)", "role": "BUILDER"}
    """

    static let working = """
    {"session_id": "63a31002-b958", "title": "Interactive Jarvis session cards build", "state": "running",
     "phase": "in_progress", "health": "working", "device": "Mini PC", "project": "jarvis-companion",
     "doing": "Building the buttons on the phone cards",
     "recap": ["I set up a safe copy of the app code", "I made the gateway store each session's card",
               "I wrote 24 checks and they all pass"],
     "last_activity_at": "\(now)", "role": "BUILDER",
     "card": {"need": null, "files": [], "links": [], "parent": null, "doing": "Building the buttons on the phone cards",
              "children": [{"session_id": "c0ffee00-1111", "what": "handed notifications", "title": "Sending phone alerts",
                            "phase": "in_progress", "health": "working", "on_board": true, "declared": true}],
              "receipt": {"text": "Also show who it handed work to", "at": "\(now)", "status": "working"}}}
    """

    static let approve = """
    {"session_id": "a1b2c3d4-0001", "title": "Plan the new inbox", "state": "idle",
     "phase": "in_review", "health": "waiting", "device": "Mini PC", "project": "jarvis-companion",
     "doing": "Waiting: your OK on the plan", "recap": ["I read the current inbox code", "I wrote a 5 step plan",
     "Needs you: Plan for the new inbox is ready. Approve it, or tell me what to change."],
     "last_activity_at": "\(now)", "role": "ARCHITECT",
     "card": {"need": {"id": "0123456789ab", "kind": "approve",
                       "question": "Plan for the new inbox is ready. Approve it, or tell me what to change.",
                       "options": [], "items": [], "file_id": "f0000000000000000000000000000001", "at": "\(now)", "answered": null},
              "files": [{"id": "f0000000000000000000000000000001", "name": "plan.md", "title": "the plan", "type": "text/markdown",
                         "view": "markdown", "size": 812, "at": "\(now)"}],
              "links": [], "children": [], "parent": null, "receipt": null, "doing": null}}
    """

    static let choice = """
    {"session_id": "a1b2c3d4-0002", "title": "Hardware Mode voice", "state": "idle",
     "phase": "in_review", "health": "waiting", "device": "Mini PC", "project": "jarvis-companion",
     "doing": "Waiting: pick a voice", "recap": ["I added two voices", "Needs you: Which voice should Jarvis use?"],
     "last_activity_at": "\(now)", "role": "BUILDER",
     "card": {"need": {"id": "0123456789ac", "kind": "choice", "question": "Which voice should Jarvis use?",
                       "options": ["Daniel", "George"], "items": [], "file_id": null, "at": "\(now)", "answered": null},
              "files": [], "links": [], "children": [], "parent": null, "doing": null,
              "receipt": {"text": "Make it sound less robotic", "at": "\(now)", "status": "answered"}}}
    """

    static let checklist = """
    {"session_id": "a1b2c3d4-0003", "title": "Ship cards build 1.0.110", "state": "idle",
     "phase": "in_review", "health": "waiting", "device": "Mini PC", "project": "jarvis-companion",
     "doing": "Waiting: test on your phone", "recap": ["I put build 1.0.110 on AltStore", "Needs you: Test this on your phone"],
     "last_activity_at": "\(now)", "role": "VERIFIER",
     "card": {"need": {"id": "0123456789ad", "kind": "checklist", "question": "Test this on your phone",
                       "options": [], "items": ["Update the app in AltStore", "Open Sessions, find a card with buttons",
                                                "Tap Approve, see 'You answered'", "Tap Revise, the chat opens ready to type"],
                       "file_id": null, "at": "\(now)", "answered": null},
              "files": [{"id": "f0000000000000000000000000000002", "name": "report.pdf", "title": "Final report", "type": "application/pdf",
                         "view": "pdf", "size": 20000, "at": "\(now)"}],
              "links": [{"url": "https://claude.ai/artifact/example", "title": "Before and after screenshots"}],
              "children": [], "parent": null, "receipt": null, "doing": null}}
    """

    static let child = """
    {"session_id": "c0ffee00-1111", "title": "Sending phone alerts", "state": "running",
     "phase": "in_progress", "health": "working", "device": "Mini PC", "project": "jarvis-companion",
     "doing": "Testing the alert when a session needs you", "recap": ["I made the alert for finished tasks"],
     "last_activity_at": "\(now)", "role": "BUILDER", "parent_session_id": null,
     "card": {"need": {"id": "0123456789ae", "kind": "approve", "question": "Plan", "options": [], "items": [],
                       "file_id": null, "at": "\(now)", "answered": {"answer": "Approve", "at": "\(now)"}},
              "files": [], "links": [], "children": [], "doing": null,
              "parent": {"session_id": "63a31002-b958", "title": "Building the buttons on the phone cards", "on_board": true},
              "receipt": {"text": "Approve", "at": "\(now)", "status": "received"}}}
    """

    static let plan = """
    # Plan: new inbox

    ## Steps
    1. Read the current inbox code
    2. **Split** mail and WhatsApp into two tabs
    3. Add a search box

    | Step | Time | Risk |
    |---|---|---|
    | Tabs | 1 h | low |
    | Search | 2 h | medium |

    - Nothing changes for old messages
    - You can undo it from Settings
    """

    // MARK: logic

    func testGatewayJSONDecodesWithCard() throws {
        let s = try Self.session(Self.approve)
        XCTAssertEqual(s.card?.need?.kind, "approve")
        XCTAssertEqual(s.card?.need?.fileId, "f0000000000000000000000000000001")
        XCTAssertEqual(s.card?.files?.first?.view, "markdown")
        let w = try Self.session(Self.working)
        XCTAssertEqual(w.card?.children?.first?.onBoard, true)
        XCTAssertEqual(w.card?.receipt?.label.text, "Being worked on")
        XCTAssertNil(try Self.session(Self.before).card)  // an older gateway: no card, still decodes
    }

    func testButtonsFitTheAsk() {
        func need(_ kind: String, options: [String] = []) -> CardInfo.Need {
            CardInfo.Need(id: "x", kind: kind, question: "q", options: options, items: [], fileId: nil, at: nil, answered: nil)
        }
        XCTAssertEqual(CardInfo.buttons(for: need("approve")).map { $0.label }, ["Approve", "Revise"])
        XCTAssertEqual(CardInfo.buttons(for: need("approve")).map { $0.sends }, [true, false])  // Revise opens the chat
        XCTAssertEqual(CardInfo.buttons(for: need("yesno")).map { $0.label }, ["Yes", "No"])
        XCTAssertEqual(CardInfo.buttons(for: need("choice", options: ["A", "B", "C"])).map { $0.label },
                       ["A", "B", "C", "Something else"])
        XCTAssertEqual(CardInfo.buttons(for: need("preview")).map { $0.label }, ["Looks good", "Change it"])
        XCTAssertEqual(CardInfo.buttons(for: need("checklist")).map { $0.label }, ["Send results", "Add a note"])
    }

    func testReceiptLabels() {
        let labels = ["sent", "received", "queued", "working", "answered", "failed"].map {
            CardInfo.Receipt(text: "t", at: nil, status: $0).label.text
        }
        XCTAssertEqual(labels, ["Sending", "Received", "Received, after the current step", "Being worked on",
                                "Answered", "Not delivered, send again"])
    }

    // MARK: screenshots

    private func boardCard(_ json: String) throws -> some View {
        let session = try Self.session(json)
        let model = SessionsModel()
        return VStack(spacing: 8) {
            SessionCard(session: session)
            if let card = session.card, card.hasContent {
                CardExtras(session: session, card: card, model: model, compact: true,
                           openChat: { _ in }, openFile: { _ in }, openSession: { _ in })
            }
        }
    }

    func testSnapshotsOfEveryCardState() throws {
        try shot("01-before-stale-card", fit: try boardCard(Self.before))
        try shot("02-working-live-bullets-child-receipt", fit: try boardCard(Self.working))
        try shot("03-plan-approve-revise", fit: try boardCard(Self.approve))
        try shot("04-choice-buttons", fit: try boardCard(Self.choice))
        try shot("05-phone-test-checklist", fit: try boardCard(Self.checklist))
        try shot("06-child-session-link-answered", fit: try boardCard(Self.child))

        let model = SessionsModel()
        model.sessions = try [Self.approve, Self.working].map(Self.session)
        try shot("07-recap-full-card", screen: RecapView(model: model, sessionID: "a1b2c3d4-0001"))
        try shot("08-recap-with-child", screen: RecapView(model: model, sessionID: "63a31002-b958"))

        let planFile = CardInfo.File(id: "f0000000000000000000000000000001", name: "plan.md", title: "the plan",
                                     type: "text/markdown", view: "markdown", size: 812)
        try shot("09-plan-attachment-markdown",
                 screen: CardFileView(target: CardFileTarget(sessionID: "a1b2c3d4-0001", file: planFile),
                                      preloaded: Data(Self.plan.utf8)))
        let pdfFile = CardInfo.File(id: "f0000000000000000000000000000002", name: "report.pdf", title: "Final report",
                                    type: "application/pdf", view: "pdf", size: 20000)
        try shot("10-report-attachment-pdf",
                 screen: CardFileView(target: CardFileTarget(sessionID: "a1b2c3d4-0003", file: pdfFile),
                                      preloaded: Self.samplePDF()))
    }

    // MARK: drawing

    private static func samplePDF() -> Data {
        let renderer = UIGraphicsPDFRenderer(bounds: CGRect(x: 0, y: 0, width: 612, height: 792))
        return renderer.pdfData { context in
            context.beginPage()
            let title = [NSAttributedString.Key.font: UIFont.boldSystemFont(ofSize: 28)]
            let body = [NSAttributedString.Key.font: UIFont.systemFont(ofSize: 16)]
            ("Final report" as NSString).draw(at: CGPoint(x: 48, y: 60), withAttributes: title)
            let lines = ["1. Do these first: update the app in AltStore.",
                         "2. What changed: cards show live steps, files and buttons.",
                         "3. Where it is saved: branch feat/session-cards."]
            for (i, line) in lines.enumerated() {
                (line as NSString).draw(at: CGPoint(x: 48, y: 120 + i * 30), withAttributes: body)
            }
        }
    }

    private func outputDir() throws -> URL {
        let env = ProcessInfo.processInfo.environment["CARD_SHOTS_DIR"]
        let dir = env.map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("card-shots")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// A board card, sized to its content at phone width.
    private func shot<V: View>(_ name: String, fit view: V) throws {
        let content = view.padding(16).frame(width: 390).background(Color.jarvisBackground)
        let probe = UIHostingController(rootView: content)
        let height = probe.sizeThatFits(in: CGSize(width: 390, height: 2000)).height
        // + room for the window's top safe area, which pushes the content down.
        try draw(name, AnyView(content.frame(maxHeight: .infinity, alignment: .top).background(Color.jarvisBackground)),
                 size: CGSize(width: 390, height: max(80, ceil(height)) + 120))
    }

    /// A full screen (sheets with their own scroll views), iPhone 14 size.
    private func shot<V: View>(_ name: String, screen view: V) throws {
        try draw(name, AnyView(view), size: CGSize(width: 390, height: 844))
    }

    private func draw(_ name: String, _ view: AnyView, size: CGSize) throws {
        let host = UIHostingController(rootView: view.preferredColorScheme(.dark))
        host.overrideUserInterfaceStyle = .dark
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        let window = scene.map { UIWindow(windowScene: $0) } ?? UIWindow()
        window.frame = CGRect(origin: .zero, size: size)
        window.rootViewController = host
        window.isHidden = false
        host.view.frame = window.bounds
        host.view.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.8))  // let SwiftUI + PDFKit lay out
        let format = UIGraphicsImageRendererFormat()
        format.scale = 3
        let image = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        window.isHidden = true
        let png = try XCTUnwrap(image.pngData())
        try png.write(to: outputDir().appendingPathComponent("\(name).png"))
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
