import SwiftUI
import WebKit

/// Central chat (framing doc §3 + addendum §6): the Sessions kanban, the chat
/// thread screen and the Floor page. Every call goes to the gateway's
/// allowlisted /chat + /central/* routes with the ios token as bearer; the
/// gateway holds the router token, the phone never sees it.

/// Home view picked from the JARVIS wordmark menu (stored in @AppStorage).
enum HomeView: String, CaseIterable {
    case controls, sessions, floor

    var label: String {
        switch self {
        case .controls: return "Controls"
        case .sessions: return "Sessions"
        case .floor: return "Floor"
        }
    }
}

/// Project line on session cards (mono purple).
extension Color {
    static let centralPurple = Color(red: 177 / 255, green: 140 / 255, blue: 255 / 255) // #B18CFF
    static let centralGreen = Color(red: 74 / 255, green: 222 / 255, blue: 128 / 255) // #4ADE80
}

struct CentralError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Request builder + status check shared by every central call. Error copy
/// matches AppModel.ask(): 401/403 → token hint; a gateway 502 carries a short
/// safe `detail` ("router unavailable", "router error N") shown verbatim.
enum Central {
    static func request(_ suffix: String, method: String = "GET",
                        json: [String: Any]? = nil, timeout: TimeInterval = 30) throws -> URLRequest {
        guard let url = AppModel.gatewayEndpoint(suffix) else {
            throw CentralError("gateway URL must be https")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        if let json = json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        return request
    }

    static func check(_ response: URLResponse, data: Data?) throws {
        guard let http = response as? HTTPURLResponse else { throw CentralError("no HTTP response") }
        if (200...299).contains(http.statusCode) { return }
        if http.statusCode == 401 || http.statusCode == 403 {
            throw CentralError("unauthorized, check the token in Settings")
        }
        if let data = data,
           let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let detail = obj["detail"] as? String {
            throw CentralError(detail)
        }
        throw CentralError("HTTP \(http.statusCode)")
    }

    static func data(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: request)
        try check(response, data: data)
        return data
    }

    static func json(_ request: URLRequest) async throws -> [String: Any] {
        let data = try await data(request)
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw CentralError("malformed reply")
        }
        return obj
    }
}

/// One router session from GET /central/sessions. The gateway always fills
/// `phase` (in_progress / in_review / done), `device`, `doing` (plain line)
/// and `recap` (plain bullets); the rest may be missing.
struct CentralSession: Identifiable, Decodable, Equatable {
    let sessionId: String
    let kind: String?
    let title: String?
    let state: String?
    let cwd: String?
    let updatedAt: String?
    let project: String?
    let task: String?
    let summary: String?
    let phase: String?
    let device: String?
    let doing: String?
    let recap: [String]?
    let lastActivityAt: String?
    let health: String?
    // Watchdog + role fields (router session ce03d92a; NOT confirmed live as
    // of this build — see .orchestrator/reports/watchdog-roles-app-side.md).
    // All optional: Codable silently leaves them nil until the router sends
    // them, same as any other field an older router omits.
    let role: String?
    let attentionReason: String?
    let doneVerdict: String?
    let doneVerdictReason: String?
    let lifecycle: String?
    let purpose: String?

    var id: String { sessionId }

    /// Contract roles (fable2-watchdog-roles-app-side.md): the sprite route's
    /// allowlist (gateway/sprites.py ROLES) falls back to WORKER for anything
    /// else, so any string here is safe to pass straight through.
    static let knownRoles: Set<String> = [
        "ARCHITECT", "RESEARCHER", "REVIEWER", "BUILDER", "SCOUT", "FIXER", "VERIFIER",
        "SECURITY", "DESIGNER", "MARKETER", "SPEC_WRITER", "GLM_WORKER", "EXPLORER", "WORKER", "OWNER",
    ]

    /// Prefer the router's own `role`; fall back to the same keyword heuristic
    /// as the Floor's roleOf() (gateway/static/floor.html) when it's missing
    /// or not one of the contract's roles. GLM rows are labelled GLM worker,
    /// never "hub" — they were deliberate smoke tests, the phone can't pick GLM.
    var resolvedRole: String {
        if let role = role, Self.knownRoles.contains(role) { return role }
        let text = ((title ?? "") + " " + (task ?? "")).lowercased()
        if kind == "glm" && !(text.contains("review") || text.contains("research")) { return "GLM_WORKER" }
        let rules: [(String, String)] = [
            ("review", "REVIEWER"), ("audit", "REVIEWER"), ("secur", "SECURITY"),
            ("verif", "VERIFIER"), ("test", "VERIFIER"),
            ("research", "RESEARCHER"), ("investigat", "RESEARCHER"), ("explor", "RESEARCHER"), ("find", "RESEARCHER"),
            ("fix", "FIXER"), ("bug", "FIXER"), ("debug", "FIXER"),
            ("design", "DESIGNER"), ("layout", "DESIGNER"),
            ("market", "MARKETER"), ("seo", "MARKETER"), ("copy", "MARKETER"), ("launch", "MARKETER"),
            ("spec", "ARCHITECT"), ("plan", "ARCHITECT"), ("architect", "ARCHITECT"),
            ("note", "SPEC_WRITER"), ("doc", "SPEC_WRITER"), ("write", "SPEC_WRITER"),
        ]
        for (needle, role) in rules where text.contains(needle) { return role }
        return "BUILDER"
    }

    /// ARCHITECT and SPEC_WRITER are both labelled "Planner" in the UI (ask 2).
    var resolvedRoleLabel: String {
        switch resolvedRole {
        case "ARCHITECT", "SPEC_WRITER": return "Planner"
        case "GLM_WORKER": return "GLM worker"
        default: return resolvedRole.capitalized
        }
    }
    var displayTask: String {
        for text in [doing, title, task] {
            if let text = text, !text.isEmpty { return text }
        }
        return sessionId
    }
    /// One line for the card: the "Needs you:" bullet first, else the first bullet.
    var recapPreview: String? {
        let items = recap ?? []
        return items.first { $0.hasPrefix("Needs you") } ?? items.first
    }
    var displayProject: String? {
        if let project = project, !project.isEmpty { return project }
        guard let cwd = cwd, !cwd.isEmpty else { return nil }
        return (cwd as NSString).lastPathComponent
    }

    /// Honest live status ("Working, last activity 20s ago" / "Waiting for you" /
    /// "Stalled, no activity 14 min"); nil for finished sessions. Router `health`
    /// and `last_activity_at` win; older routers fall back to phase + updated_at.
    func statusLine(now: Date) -> (text: String, color: Color)? {
        let ago = CentralTime.parse(lastActivityAt ?? updatedAt).map { CentralTime.ago($0, now: now) }
        let fallback = phase == "in_review" ? "waiting" : phase == "in_progress" ? "working" : nil
        // New health enum: working | quiet | waiting | idle | done | attention | failed
        // (router session ce03d92a). "waiting_for_owner"/"stalled" are the old
        // values, kept so an older router's cache doesn't go blank mid-rollout.
        switch health ?? fallback ?? "" {
        case "attention":
            return (attentionReason.map { "Needs attention: \($0)" } ?? "Needs attention", .jarvisDanger)
        case "waiting", "waiting_for_owner":
            return ("Waiting for you", .jarvisAccent)
        case "stalled", "failed":
            return (ago.map { "Stalled, no activity \($0)" } ?? "Stalled", .jarvisDanger)
        case "quiet":
            // Busy but silent, NOT stuck — this is the "fixes DONE card showing
            // 'Working, last activity...'" case from the contract: a quiet
            // session never shows "Working" so it stops looking stalled.
            return (ago.map { "Quiet, last activity \($0) ago" } ?? "Quiet", .jarvisMuted)
        case "idle":
            return ("Idle", .jarvisMuted)
        case "working":
            return (ago.map { "Working, last activity \($0) ago" } ?? "Working", .centralGreen)
        default:
            // A done session whose done_verdict says the DONE might be false
            // still gets a status line even though "done" otherwise shows none.
            if phase == "done" && doneVerdict == "suspect" {
                return (doneVerdictReason.map { "Check this DONE: \($0)" } ?? "Check this DONE", .jarvisDanger)
            }
            return nil
        }
    }
}

/// The gateway's ISO 8601 UTC times, with or without fractional seconds.
enum CentralTime {
    private static let whole = ISO8601DateFormatter()
    private static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    static func parse(_ text: String?) -> Date? {
        guard let text = text, !text.isEmpty else { return nil }
        return whole.date(from: text) ?? fractional.date(from: text)
    }

    static func ago(_ date: Date, now: Date) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(date)))
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60) min" }
        if seconds < 86400 { return "\(seconds / 3600) h" }
        return "\(seconds / 86400) d"
    }
}

/// The live status line; re-renders every 5 s on its own so "20s ago" keeps
/// counting between polls without a refetch.
struct StatusLine: View {
    let session: CentralSession

    var body: some View {
        TimelineView(.periodic(from: .now, by: 5)) { context in
            if let status = session.statusLine(now: context.date) {
                HStack(spacing: 6) {
                    Circle().fill(status.color).frame(width: 6, height: 6)
                    Text(status.text)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(status.color)
                        .lineLimit(1)
                }
            }
        }
    }
}

/// Which session's recap sheet is open.
struct RecapTarget: Identifiable {
    let id: String
}

/// What a chat sheet opens on: an existing session, or nil for a new thread.
struct ChatTarget: Identifiable {
    let id = UUID()
    let sessionID: String?
    let title: String
}

@MainActor
final class SessionsModel: ObservableObject {
    @Published var sessions: [CentralSession] = []
    @Published var error: String?

    func refresh() async {
        do {
            let data = try await Central.data(Central.request("/central/sessions"))
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            sessions = try decoder.decode([CentralSession].self, from: data)
            error = nil
        } catch is CancellationError {
            return
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Hide every done session (gateway stores the time); reactivated ones come back.
    func clearDone() async {
        do {
            _ = try await Central.data(Central.request("/central/clear-done", method: "POST"))
            await refresh()
        } catch is CancellationError {
            return
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// Sessions kanban: three columns paged horizontally, each ~85% wide so the
/// next column peeks in. Refreshes every 5 s while on screen and the app is
/// in the foreground (`.task(id: scenePhase)` restarts/cancels the loop), and
/// on pull; cards move columns and update in place by session id. Tapping a
/// card opens its recap; the recap has "Open chat".
struct SessionsBoard: View {
    @StateObject private var model = SessionsModel()
    @State private var openChat: ChatTarget?
    @State private var openRecap: RecapTarget?
    @Environment(\.scenePhase) private var scenePhase

    private struct Column {
        let key: String
        let label: String
        let matches: (CentralSession) -> Bool
    }

    // ATTENTION pulls out of whichever phase column a session would otherwise
    // sit in — health == "attention" is more urgent than a plain in_progress/
    // in_review bucket, so it gets its own lane instead of hiding inside one
    // of those (ask 4: "attention column/badge").
    private static let columns = [
        Column(key: "in_progress", label: "IN PROGRESS") { $0.phase == "in_progress" && $0.health != "attention" },
        Column(key: "in_review", label: "IN REVIEW") { $0.phase == "in_review" && $0.health != "attention" },
        Column(key: "attention", label: "ATTENTION") { $0.health == "attention" },
        Column(key: "done", label: "DONE") { $0.phase == "done" },
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                if let error = model.error {
                    Text(error)
                        .font(.footnote)
                        .foregroundColor(.jarvisDanger)
                        .lineLimit(2)
                }
                Spacer(minLength: 8)
                Button {
                    Task { await model.clearDone() }
                } label: {
                    Label("Clear done", systemImage: "checkmark.circle")
                        .font(.subheadline)
                        .foregroundColor(.jarvisMuted)
                        .frame(minHeight: 44)
                }
                Button {
                    openChat = ChatTarget(sessionID: nil, title: "New chat")
                } label: {
                    Label("New chat", systemImage: "plus")
                        .font(.subheadline)
                        .foregroundColor(.jarvisAccent)
                        .frame(minHeight: 44)
                }
            }
            GeometryReader { geo in
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 12) {
                        ForEach(Self.columns, id: \.key) { column in
                            self.column(column)
                                .frame(width: geo.size.width * 0.85)
                        }
                    }
                    .modifier(ColumnTargets())
                }
                .modifier(ColumnPaging())
            }
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                await model.refresh()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
        .sheet(item: $openChat) { ChatView(target: $0) }
        .sheet(item: $openRecap) { RecapView(model: model, sessionID: $0.id) }
    }

    private func column(_ column: Column) -> some View {
        let items = model.sessions.filter(column.matches)
        return VStack(alignment: .leading, spacing: 8) {
            Text("\(column.label) · \(items.count)")
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .tracking(1)
                .foregroundColor(.jarvisMuted)
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(items) { session in
                        Button {
                            openRecap = RecapTarget(id: session.sessionId)
                        } label: {
                            SessionCard(session: session)
                        }
                        .buttonStyle(.plain)
                    }
                    if items.isEmpty {
                        Text("nothing here")
                            .font(.footnote)
                            .foregroundColor(.jarvisMuted)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 8)
                    }
                }
            }
            .refreshable { await model.refresh() }
        }
    }
}

/// Snap columns into place on iOS 17+; iOS 16 scrolls freely (no API there).
private struct ColumnPaging: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 17.0, *) {
            content.scrollTargetBehavior(.viewAligned)
        } else {
            content
        }
    }
}

private struct ColumnTargets: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 17.0, *) {
            content.scrollTargetLayout()
        } else {
            content
        }
    }
}

/// Card: what it's doing (bold), device chip + project (mono purple), one
/// plain recap line (else the summary, 2 lines); radius 12 + 1 px border.
/// done → "done ✓" green, in review → "needs you" orange, attention → red,
/// a done session with done_verdict "suspect" → amber "check" instead of ✓.
struct SessionCard: View {
    let session: CentralSession

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                RoleSprite(role: session.resolvedRole)
                    .accessibilityLabel(session.resolvedRoleLabel)
                Text(session.displayTask)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(.jarvisText)
                    .lineLimit(2)
                Spacer(minLength: 6)
                badge
            }
            StatusLine(session: session)
            HStack(spacing: 6) {
                if let device = session.device, !device.isEmpty {
                    DeviceChip(device: device)
                }
                if let project = session.displayProject {
                    Text(project)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundColor(.centralPurple)
                        .lineLimit(1)
                }
            }
            if let line = session.recapPreview ?? session.summary, !line.isEmpty {
                Text(line)
                    .font(.footnote)
                    .foregroundColor(.jarvisMuted)
                    .lineLimit(2)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.jarvisSurface))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white.opacity(0.08), lineWidth: 1))
        .contentShape(Rectangle())
    }

    @ViewBuilder
    private var badge: some View {
        if session.health == "attention" {
            badgeText("attention", color: .jarvisDanger)
        } else if session.state == "failed" || session.health == "failed" {
            badgeText("failed", color: .jarvisDanger)
        } else if session.phase == "done" {
            if session.doneVerdict == "suspect" {
                badgeText("done? check", color: .jarvisDanger)
            } else {
                badgeText("done ✓", color: .centralGreen)
            }
        } else if session.phase == "in_review" {
            badgeText("needs you", color: .jarvisAccent)
        }
    }

    private func badgeText(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold, design: .monospaced))
            .foregroundColor(color)
            .lineLimit(1)
    }
}

struct DeviceChip: View {
    let device: String

    var body: some View {
        Text(device)
            .font(.system(size: 11, weight: .semibold))
            .foregroundColor(.jarvisText)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.white.opacity(0.08)))
    }
}

// MARK: - Role sprites (ask 2)
//
// gateway/sprites.py is the single source of truth for the pixel grid; the
// Floor (web canvas) loads its SVG, but SwiftUI can't decode SVG fetched from
// the network, so the board instead fetches the plain colour-grid JSON twin
// at GET /central/sprites/{role}.json and draws it cell by cell with Canvas.

extension Color {
    /// "#RRGGBB" only (what gateway/sprites.py emits) — nil for anything else.
    init?(hex: String) {
        var s = hex
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let value = UInt32(s, radix: 16) else { return nil }
        self.init(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}

struct SpriteGrid: Decodable {
    let w: Int
    let h: Int
    let role: String
    let rows: [[String?]]
}

/// Process-lifetime cache: every role's grid is small and immutable (the
/// gateway serves it with a 24h Cache-Control too), so one fetch per role
/// per app launch is enough — no need to refetch per card.
@MainActor
final class SpriteCache: ObservableObject {
    static let shared = SpriteCache()
    @Published private(set) var grids: [String: SpriteGrid] = [:]
    private var inflight: Set<String> = []

    func load(_ role: String) {
        guard grids[role] == nil, !inflight.contains(role) else { return }
        inflight.insert(role)
        Task {
            defer { inflight.remove(role) }
            guard let request = try? Central.request("/central/sprites/\(role).json") else { return }
            guard let data = try? await Central.data(request) else { return }
            guard let grid = try? JSONDecoder().decode(SpriteGrid.self, from: data) else { return }
            grids[role] = grid
        }
    }
}

/// One role's pixel sprite, drawn at card-icon size. Falls back to a plain
/// tinted square while the grid is loading (first appearance only — cached
/// after that), never to a blank space, so a slow/offline gateway doesn't
/// make role identity disappear.
struct RoleSprite: View {
    let role: String
    @ObservedObject private var cache = SpriteCache.shared

    var body: some View {
        Canvas { context, size in
            guard let grid = cache.grids[role] else {
                context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(.jarvisMuted.opacity(0.3)))
                return
            }
            let cw = size.width / CGFloat(grid.w)
            let ch = size.height / CGFloat(grid.h)
            for (y, row) in grid.rows.enumerated() {
                for (x, hex) in row.enumerated() {
                    guard let hex = hex, let color = Color(hex: hex) else { continue }
                    let rect = CGRect(x: CGFloat(x) * cw, y: CGFloat(y) * ch, width: cw + 0.5, height: ch + 0.5)
                    context.fill(Path(rect), with: .color(color))
                }
            }
        }
        .frame(width: 20, height: 22)
        .onAppear { cache.load(role) }
    }
}

/// Recap sheet: what the session is doing + short plain bullets ("I did…",
/// "Needs you: …"). Reads the shared SessionsModel, so the 5 s poll keeps it
/// live; "Open chat" opens the thread on top.
struct RecapView: View {
    @ObservedObject var model: SessionsModel
    let sessionID: String
    @Environment(\.dismiss) private var dismiss
    @State private var openChat: ChatTarget?

    private var session: CentralSession? {
        model.sessions.first { $0.sessionId == sessionID }
    }

    var body: some View {
        NavigationView {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let session = session {
                        Text(session.displayTask)
                            .font(.title3.weight(.semibold))
                            .foregroundColor(.jarvisText)
                        StatusLine(session: session)
                        HStack(spacing: 6) {
                            if let device = session.device, !device.isEmpty {
                                DeviceChip(device: device)
                            }
                            if let project = session.displayProject {
                                Text(project)
                                    .font(.system(size: 12, design: .monospaced))
                                    .foregroundColor(.centralPurple)
                            }
                        }
                        let bullets = session.recap ?? []
                        if bullets.isEmpty {
                            Text(session.summary ?? "No recap yet.")
                                .font(.body)
                                .foregroundColor(.jarvisMuted)
                        } else {
                            ForEach(Array(bullets.enumerated()), id: \.offset) { _, line in
                                HStack(alignment: .firstTextBaseline, spacing: 8) {
                                    Text("•").foregroundColor(.jarvisMuted)
                                    Text(line)
                                        .font(.body)
                                        .foregroundColor(line.hasPrefix("Needs you") ? .jarvisAccent : .jarvisText)
                                }
                            }
                        }
                        Button {
                            openChat = ChatTarget(sessionID: session.sessionId, title: session.displayTask)
                        } label: {
                            Label("Open chat", systemImage: "bubble.left.and.bubble.right")
                                .font(.headline)
                                .frame(maxWidth: .infinity, minHeight: 44)
                        }
                        .buttonStyle(.borderedProminent)
                        .padding(.top, 8)
                    } else {
                        Text("This session is gone.")
                            .foregroundColor(.jarvisMuted)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .navigationTitle("Recap")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
        .sheet(item: $openChat) { ChatView(target: $0) }
    }
}

struct ChatLine: Identifiable, Equatable {
    enum Role { case owner, jarvis, status, error }
    let id = UUID()
    let role: Role
    var text: String
    /// Router action chip on owner lines: resumed / spawned / answered / needs_input / queued.
    var chip: String?
}

/// One chat thread. A new thread posts to /chat (thread_id "app-<8 hex>");
/// once a session exists, messages go to /central/sessions/{id}/message. Each
/// send gets one msg_id, reused on Retry (the router is idempotent on it). The
/// SSE loop resumes with Last-Event-ID (the integer seq) and keeps listening
/// across turns (every turn ends with `done` or `error`) until a 401/403, close(), or
/// maxReconnects quiet attempts while a turn is still being answered.
@MainActor
final class ChatModel: ObservableObject {
    @Published var lines: [ChatLine] = []
    @Published var options: [String] = []
    @Published var sessionID: String?
    @Published var title: String
    @Published var isSending = false
    @Published var canRetry = false

    let threadID = "app-" + String(UUID().uuidString.prefix(8)).lowercased()
    private var pendingMsgID: String?
    private var failed: (text: String, ownerIndex: Int)?
    /// Highest seq seen; nil until the first event (seq 0 is a real event).
    private var lastSeq: Int?
    /// Seq of every rendered event: a replay (fresh open of an old session, a
    /// reconnect) renders each event once, so old history shows once as history.
    private var seenSeqs: Set<Int> = []
    /// Texts /chat rendered without a seq; the stream's replay of each is skipped
    /// once (fallback for a router that sends no `seq` on /route).
    private var echoed: [String] = []
    /// A sent message is still being answered; only then do quiet reconnects count.
    private var turnActive = false
    /// The live loop holds an open SSE connection (cleared by whoever cancels it).
    private var streamConnected = false
    private var streamTask: Task<Void, Never>?
    /// Set by close(): no stream may start after the sheet went away.
    private var closed = false
    /// Reconnects in a row with no event before giving up.
    private static let maxReconnects = 8

    private enum StreamEnd { case unauthorized, dropped(gotEvent: Bool) }

    init(sessionID: String?, title: String) {
        self.sessionID = sessionID
        self.title = title
    }

    func send(_ raw: String) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }
        lines.append(ChatLine(role: .owner, text: text))
        pendingMsgID = UUID().uuidString
        deliver(text, ownerIndex: lines.count - 1)
    }

    /// Resend the failed message with the SAME msg_id.
    func retry() {
        guard let failed = failed, !isSending else { return }
        deliver(failed.text, ownerIndex: failed.ownerIndex)
    }

    private func deliver(_ text: String, ownerIndex: Int) {
        guard let msgID = pendingMsgID else { return }
        failed = nil
        canRetry = false
        options = []
        isSending = true
        // Set before the POST: this turn's `done` may arrive on the open stream first.
        turnActive = true
        Task {
            defer { isSending = false }
            do {
                if let sid = sessionID {
                    let obj = try await Central.json(Central.request(
                        "/central/sessions/\(sid)/message", method: "POST",
                        json: ["msg_id": msgID, "text": text]
                    ))
                    lines[ownerIndex].chip = obj["action"] as? String
                } else {
                    let obj = try await Central.json(Central.request(
                        "/chat", method: "POST",
                        json: ["thread_id": threadID, "msg_id": msgID, "text": text], timeout: 45
                    ))
                    apply(route: obj, ownerIndex: ownerIndex)
                }
                pendingMsgID = nil
                Haptics.success()
                wakeStream()
            } catch {
                turnActive = false
                failed = (text: text, ownerIndex: ownerIndex)
                canRetry = true
                lines.append(ChatLine(role: .error, text: "send failed: \(error.localizedDescription)"))
                Haptics.error()
            }
        }
    }

    private func apply(route obj: [String: Any], ownerIndex: Int) {
        let action = obj["action"] as? String
        lines[ownerIndex].chip = action
        if let sid = obj["session_id"] as? String, !sid.isEmpty { sessionID = sid }
        if let title = obj["title"] as? String, !title.isEmpty { self.title = title }
        // The stream replays this turn's reply: mark its seq rendered and resume after it.
        let seq = obj["seq"] as? Int
        if let seq = seq {
            seenSeqs.insert(seq)
            lastSeq = max(lastSeq ?? seq, seq)
        }
        if let reply = obj["reply"] as? String, !reply.isEmpty {
            lines.append(ChatLine(role: .jarvis, text: reply))
            if seq == nil { echoed.append(reply) }
        }
        if action == "needs_input" {
            if let prompt = obj["prompt"] as? String, !prompt.isEmpty {
                lines.append(ChatLine(role: .jarvis, text: prompt))
                if seq == nil { echoed.append(prompt) }
            }
            options = obj["options"] as? [String] ?? []
        }
    }

    /// The sheet appeared: allow streaming again and start it.
    func open() {
        closed = false
        startStream()
    }

    func startStream() {
        guard !closed, streamTask == nil, let sid = sessionID else { return }
        streamTask = Task { [weak self] in
            var delay: UInt64 = 1
            var quietAttempts = 0
            while !Task.isCancelled {
                guard let end = await self?.streamOnce(sid) else { return }
                guard case .dropped(let gotEvent) = end else {
                    self?.streamEnded()
                    return
                }
                // Only a connection that delivered something resets the backoff.
                if gotEvent {
                    delay = 1
                    quietAttempts = 0
                }
                // Between turns a quiet stream is normal: keep listening, never give up.
                if self?.turnActive == true {
                    quietAttempts += 1
                } else {
                    quietAttempts = 0
                }
                if quietAttempts >= ChatModel.maxReconnects {
                    self?.lines.append(ChatLine(role: .error, text: "live updates lost, reopen the chat"))
                    self?.streamEnded()
                    return
                }
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                delay = min(delay * 2, 30)
            }
        }
    }

    /// After a send: a loop asleep between reconnects would show the new turn
    /// late, so replace it with a fresh one. An open connection is kept.
    private func wakeStream() {
        guard !streamConnected else { return }
        streamTask?.cancel()
        streamTask = nil
        startStream()
    }

    /// The loop ended on its own; a later send may start a new one.
    /// A cancelled loop leaves streamTask alone (close() or a newer loop owns it).
    private func streamEnded() {
        if !Task.isCancelled { streamTask = nil }
    }

    func close() {
        closed = true
        streamTask?.cancel()
        streamTask = nil
        streamConnected = false
    }

    func stop() {
        guard let sid = sessionID else { return }
        Task {
            do {
                _ = try await Central.json(Central.request("/central/sessions/\(sid)/stop", method: "POST"))
                lines.append(ChatLine(role: .status, text: "stopped"))
            } catch {
                lines.append(ChatLine(role: .error, text: "stop failed: \(error.localizedDescription)"))
            }
        }
    }

    /// One SSE connection. `bytes.lines` drops blank lines, so an event is
    /// dispatched on its `data:` line (the contract sends one-line JSON);
    /// the preceding `id:` becomes Last-Event-ID for the next reconnect.
    private func streamOnce(_ sid: String) async -> StreamEnd {
        var gotEvent = false
        // A cancelled loop leaves the flag to close()/wakeStream(), which reset it.
        defer { if !Task.isCancelled { streamConnected = false } }
        do {
            var request = try Central.request("/central/sessions/\(sid)/events", timeout: 660)
            request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
            // Integer only: the gateway drops any non-numeric Last-Event-ID anyway.
            if let last = lastSeq { request.setValue(String(last), forHTTPHeaderField: "Last-Event-ID") }
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 401 || http.statusCode == 403 {
                lines.append(ChatLine(role: .error, text: "unauthorized, check the token in Settings"))
                return .unauthorized
            }
            try Central.check(response, data: nil)
            streamConnected = true
            var eventID: String?
            for try await line in bytes.lines {
                if line.hasPrefix("id:") {
                    eventID = line.dropFirst(3).trimmingCharacters(in: .whitespaces)
                } else if line.hasPrefix("data:") {
                    gotEvent = true
                    handle(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces), id: eventID)
                    eventID = nil
                }
            }
        } catch {
            // Network drop, timeout or cancel: the caller decides whether to retry.
        }
        return .dropped(gotEvent: gotEvent)
    }

    /// Applies one event; junk and already-rendered seqs are ignored.
    private func handle(_ payload: String, id: String?) {
        guard let data = payload.data(using: .utf8),
              let event = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        // Replays are dropped by seq; the SSE `id:` fills in a missing seq.
        if let seq = event["seq"] as? Int ?? id.flatMap({ Int($0) }) {
            guard seenSeqs.insert(seq).inserted else { return }
            lastSeq = max(lastSeq ?? seq, seq)
        }
        let type = event["type"] as? String
        // Option buttons only live while the session is waiting on them.
        if type != "needs_input" { options = [] }
        let text = event["text"] as? String ?? ""
        switch type {
        case "text":
            if !text.isEmpty, !alreadyShown(text) { lines.append(ChatLine(role: .jarvis, text: text)) }
        case "status":
            if !text.isEmpty { lines.append(ChatLine(role: .status, text: text)) }
        case "needs_input":
            let prompt = event["prompt"] as? String ?? text
            if !prompt.isEmpty, !alreadyShown(prompt) { lines.append(ChatLine(role: .jarvis, text: prompt)) }
            options = event["options"] as? [String] ?? []
        case "error":
            // Terminal for the turn: show it once, then idle like after `done`
            // (no quiet-reconnect countdown for a turn that already failed).
            turnActive = false
            lines.append(ChatLine(role: .error, text: text.isEmpty ? "session error" : text))
            echoed = []
        case "done":
            turnActive = false
            if !text.isEmpty, !alreadyShown(text) { lines.append(ChatLine(role: .jarvis, text: text)) }
            echoed = []
        default:
            break
        }
    }

    /// True once for a text /chat already rendered; that copy is then forgotten.
    private func alreadyShown(_ text: String) -> Bool {
        guard let index = echoed.firstIndex(of: text) else { return false }
        echoed.remove(at: index)
        return true
    }
}

/// Chat thread sheet: messages with a per-message action chip, needs_input
/// option buttons, Retry on a failed send, Stop for a live session.
struct ChatView: View {
    @StateObject private var model: ChatModel
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""

    init(target: ChatTarget) {
        _model = StateObject(wrappedValue: ChatModel(sessionID: target.sessionID, title: target.title))
    }

    var body: some View {
        VStack(spacing: 12) {
            header
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(model.lines) { line in
                            lineView(line).id(line.id)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: model.lines.count) { _ in
                    if let last = model.lines.last?.id {
                        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(last, anchor: .bottom) }
                    }
                }
            }
            if !model.options.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(model.options, id: \.self) { option in
                            Button { model.send(option) } label: { pill(option) }
                                .disabled(model.isSending)
                        }
                    }
                }
            }
            if model.canRetry {
                Button { model.retry() } label: { pill("Retry") }
                    .disabled(model.isSending)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            inputRow
        }
        .padding(16)
        .background(Color.jarvisBackground.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .onAppear { model.open() }
        .onDisappear { model.close() }
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.title)
                    .font(.headline)
                    .foregroundColor(.jarvisText)
                    .lineLimit(1)
                Text(model.sessionID ?? model.threadID)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(.jarvisMuted)
                    .lineLimit(1)
            }
            Spacer()
            if model.sessionID != nil {
                Button("Stop") { model.stop() }
                    .font(.body.weight(.medium))
                    .foregroundColor(.jarvisDanger)
                    .frame(minHeight: 44)
            }
            Button("Done") { dismiss() }
                .font(.body.weight(.medium))
                .foregroundColor(.jarvisAccent)
                .frame(minHeight: 44)
        }
    }

    @ViewBuilder
    private func lineView(_ line: ChatLine) -> some View {
        switch line.role {
        case .owner:
            VStack(alignment: .leading, spacing: 4) {
                Text(line.text)
                    .font(.body)
                    .foregroundColor(.jarvisMuted)
                    .textSelection(.enabled)
                if let chip = line.chip {
                    Text(chip.replacingOccurrences(of: "_", with: " "))
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundColor(.jarvisMuted)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(Color.jarvisSurface))
                }
            }
        case .jarvis:
            Text(line.text)
                .font(.body)
                .foregroundColor(.jarvisText)
                .textSelection(.enabled)
        case .status:
            Text(line.text)
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(.jarvisMuted)
        case .error:
            Text(line.text)
                .font(.footnote)
                .foregroundColor(.jarvisDanger)
        }
    }

    private func pill(_ text: String) -> some View {
        Text(text)
            .font(.subheadline)
            .foregroundColor(.jarvisText)
            .lineLimit(1)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
    }

    private var inputRow: some View {
        HStack(spacing: 8) {
            TextField("message…", text: $draft, prompt: Text("message…").foregroundColor(.jarvisMuted))
                .font(.body)
                .foregroundColor(.jarvisText)
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
                .submitLabel(.send)
                .onSubmit(sendDraft)
            Button(action: sendDraft) {
                Group {
                    if model.isSending {
                        ProgressView().tint(.jarvisAccent)
                    } else {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 30))
                            .foregroundColor(.jarvisAccent)
                    }
                }
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
            }
            .disabled(model.isSending)
            .accessibilityLabel("Send")
        }
    }

    private func sendDraft() {
        guard !model.isSending, !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        model.send(draft)
        draft = ""
    }
}

/// Floor: the gateway's /central/floor page (the Agency Floor) in its own
/// WKWebView. The page holds no data; it fetches /central/* itself with the
/// bearer injected below as `window.__JARVIS` (never in a URL or storage).
/// "Open chat" in its side sheet posts `{type:"openChat", session_id, title}`
/// to the `jarvis` handler. A 404 means the gateway predates the page.
struct FloorView: View {
    var onOpenChat: (ChatTarget) -> Void = { _ in }
    @State private var comingSoon = false

    var body: some View {
        if !comingSoon, let request = Self.pageRequest() {
            FloorWebView(request: request, onNotFound: { comingSoon = true }, onOpenChat: onOpenChat)
                .clipShape(RoundedRectangle(cornerRadius: jarvisRadius))
        } else {
            Text(comingSoon ? "Floor coming soon" : "set gateway in settings")
                .font(.body)
                .foregroundColor(.jarvisMuted)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    /// The page itself is public and holds no data: load it without the bearer,
    /// which only reaches the page's own /central/* fetches via `window.__JARVIS`.
    private static func pageRequest() -> URLRequest? {
        guard var request = try? Central.request("/central/floor") else { return nil }
        request.setValue(nil, forHTTPHeaderField: "Authorization")
        return request
    }
}

private struct FloorWebView: UIViewRepresentable {
    let request: URLRequest
    let onNotFound: () -> Void
    let onOpenChat: (ChatTarget) -> Void

    static let handlerName = "jarvis"

    func makeCoordinator() -> Coordinator {
        Coordinator(origin: request.url, onNotFound: onNotFound, onOpenChat: onOpenChat)
    }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let controller = WKUserContentController()
        controller.addUserScript(WKUserScript(source: Self.bootstrap(), injectionTime: .atDocumentStart,
                                              forMainFrameOnly: true))
        // The controller retains its handler; the proxy keeps that a weak ref (no cycle).
        controller.add(WeakScriptHandler(context.coordinator), name: Self.handlerName)
        config.userContentController = controller
        config.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: config)
        view.isOpaque = false
        view.backgroundColor = UIColor(Color.jarvisBackground)
        view.navigationDelegate = context.coordinator
        view.load(request)
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        context.coordinator.onOpenChat = onOpenChat
    }

    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        uiView.configuration.userContentController.removeScriptMessageHandler(forName: handlerName)
        uiView.configuration.userContentController.removeAllUserScripts()
    }

    /// `window.__JARVIS = {token, base}` as a JSON literal (escaped by JSONSerialization).
    static func bootstrap() -> String {
        let payload: [String: String] = ["token": AppSettings.token, "base": ""]
        let json = (try? JSONSerialization.data(withJSONObject: payload))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        return "window.__JARVIS = Object.freeze(\(json));"
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        let origin: URL?
        let onNotFound: () -> Void
        var onOpenChat: (ChatTarget) -> Void

        init(origin: URL?, onNotFound: @escaping () -> Void, onOpenChat: @escaping (ChatTarget) -> Void) {
            self.origin = origin
            self.onNotFound = onNotFound
            self.onOpenChat = onOpenChat
        }

        /// The bearer is injected into every main-frame page, so stay on the gateway origin.
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            let url = navigationAction.request.url
            let same = url?.scheme == origin?.scheme && url?.host == origin?.host && url?.port == origin?.port
            decisionHandler(same ? .allow : .cancel)
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            if navigationResponse.isForMainFrame,
               let http = navigationResponse.response as? HTTPURLResponse, http.statusCode == 404 {
                decisionHandler(.cancel)
                DispatchQueue.main.async { self.onNotFound() }
                return
            }
            decisionHandler(.allow)
        }

        func userContentController(_ userContentController: WKUserContentController,
                                   didReceive message: WKScriptMessage) {
            guard message.frameInfo.isMainFrame,
                  let body = message.body as? [String: Any],
                  body["type"] as? String == "openChat",
                  let sid = body["session_id"] as? String,
                  sid.range(of: "^[A-Za-z0-9-]{1,64}$", options: .regularExpression) != nil
            else { return }
            let title = (body["title"] as? String).map { String($0.prefix(80)) } ?? "Chat"
            onOpenChat(ChatTarget(sessionID: sid, title: title))
        }
    }
}

/// Forwards script messages to a weakly held handler so the user content
/// controller (which retains its handlers) does not keep the coordinator alive.
private final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        target?.userContentController(userContentController, didReceive: message)
    }
}
