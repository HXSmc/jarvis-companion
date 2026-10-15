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
/// `phase` (in_progress / in_review / done); the rest may be missing.
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

    var id: String { sessionId }
    var displayTask: String { task ?? title ?? sessionId }
    var displayProject: String? {
        if let project = project, !project.isEmpty { return project }
        guard let cwd = cwd, !cwd.isEmpty else { return nil }
        return (cwd as NSString).lastPathComponent
    }
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
}

/// Sessions kanban: three columns paged horizontally, each ~85% wide so the
/// next column peeks in. Refreshes every 10 s while on screen (`.task` is
/// cancelled when the view goes away) and on pull. Tapping a card opens chat.
struct SessionsBoard: View {
    @StateObject private var model = SessionsModel()
    @State private var openChat: ChatTarget?

    private struct Column {
        let phase: String
        let label: String
    }

    private static let columns = [
        Column(phase: "in_progress", label: "IN PROGRESS"),
        Column(phase: "in_review", label: "IN REVIEW"),
        Column(phase: "done", label: "DONE"),
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
                        ForEach(Self.columns, id: \.phase) { column in
                            self.column(column.phase, label: column.label)
                                .frame(width: geo.size.width * 0.85)
                        }
                    }
                    .modifier(ColumnTargets())
                }
                .modifier(ColumnPaging())
            }
        }
        .task {
            while !Task.isCancelled {
                await model.refresh()
                try? await Task.sleep(nanoseconds: 10_000_000_000)
            }
        }
        .sheet(item: $openChat) { ChatView(target: $0) }
    }

    private func column(_ phase: String, label: String) -> some View {
        let items = model.sessions.filter { $0.phase == phase }
        return VStack(alignment: .leading, spacing: 8) {
            Text("\(label) · \(items.count)")
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .tracking(1)
                .foregroundColor(.jarvisMuted)
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(items) { session in
                        Button {
                            openChat = ChatTarget(sessionID: session.sessionId, title: session.displayTask)
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

/// Card: task (bold), project (mono purple), summary (2 lines); radius 12 +
/// 1 px border. done → "done ✓" green, in review → "needs you" orange.
struct SessionCard: View {
    let session: CentralSession

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(session.displayTask)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(.jarvisText)
                    .lineLimit(2)
                Spacer(minLength: 6)
                badge
            }
            if let project = session.displayProject {
                Text(project)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundColor(.centralPurple)
                    .lineLimit(1)
            }
            if let summary = session.summary, !summary.isEmpty {
                Text(summary)
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
        if session.state == "failed" {
            badgeText("failed", color: .jarvisDanger)
        } else if session.phase == "done" {
            badgeText("done ✓", color: .centralGreen)
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
/// SSE loop resumes with Last-Event-ID and runs until close().
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
    private var lastEventID: String?
    private var lastSeq = 0
    private var streamTask: Task<Void, Never>?

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
                startStream()
            } catch {
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
        if let reply = obj["reply"] as? String, !reply.isEmpty {
            lines.append(ChatLine(role: .jarvis, text: reply))
        }
        if action == "needs_input" {
            if let prompt = obj["prompt"] as? String, !prompt.isEmpty {
                lines.append(ChatLine(role: .jarvis, text: prompt))
            }
            options = obj["options"] as? [String] ?? []
        }
    }

    func startStream() {
        guard streamTask == nil, let sid = sessionID else { return }
        streamTask = Task {
            var delay: UInt64 = 1
            while !Task.isCancelled {
                do {
                    try await streamOnce(sid)
                    delay = 1
                } catch {
                    delay = min(delay * 2, 30)
                }
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            }
        }
    }

    func close() {
        streamTask?.cancel()
        streamTask = nil
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
    private func streamOnce(_ sid: String) async throws {
        var request = try Central.request("/central/sessions/\(sid)/events", timeout: 660)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if let last = lastEventID { request.setValue(last, forHTTPHeaderField: "Last-Event-ID") }
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        try Central.check(response, data: nil)
        var eventID: String?
        for try await line in bytes.lines {
            if line.hasPrefix("id:") {
                eventID = line.dropFirst(3).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("data:") {
                handle(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces), id: eventID)
                eventID = nil
            }
        }
    }

    private func handle(_ payload: String, id: String?) {
        guard let data = payload.data(using: .utf8),
              let event = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        let seq = event["seq"] as? Int
        // Replays after a resume are dropped by seq.
        if let seq = seq {
            if seq <= lastSeq { return }
            lastSeq = seq
        }
        if let resumeID = id ?? seq.map { String($0) } { lastEventID = resumeID }
        let text = event["text"] as? String ?? ""
        switch event["type"] as? String {
        case "text":
            if !text.isEmpty { lines.append(ChatLine(role: .jarvis, text: text)) }
        case "status":
            if !text.isEmpty { lines.append(ChatLine(role: .status, text: text)) }
        case "needs_input":
            let prompt = event["prompt"] as? String ?? text
            if !prompt.isEmpty { lines.append(ChatLine(role: .jarvis, text: prompt)) }
            options = event["options"] as? [String] ?? []
        case "error":
            lines.append(ChatLine(role: .error, text: text.isEmpty ? "session error" : text))
        case "done":
            if !text.isEmpty { lines.append(ChatLine(role: .jarvis, text: text)) }
        default:
            break
        }
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
        .onAppear { model.startStream() }
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

/// Floor: the gateway's /central/floor page in its own WKWebView (bearer on
/// the first request). A 404 means the page isn't built yet.
struct FloorView: View {
    @State private var comingSoon = false

    var body: some View {
        if !comingSoon, let request = try? Central.request("/central/floor") {
            FloorWebView(request: request) { comingSoon = true }
                .clipShape(RoundedRectangle(cornerRadius: jarvisRadius))
        } else {
            Text(comingSoon ? "Floor coming soon" : "set gateway in settings")
                .font(.body)
                .foregroundColor(.jarvisMuted)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct FloorWebView: UIViewRepresentable {
    let request: URLRequest
    let onNotFound: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onNotFound: onNotFound) }

    func makeUIView(context: Context) -> WKWebView {
        let view = WKWebView()
        view.isOpaque = false
        view.backgroundColor = UIColor(Color.jarvisBackground)
        view.navigationDelegate = context.coordinator
        view.load(request)
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate {
        let onNotFound: () -> Void
        init(onNotFound: @escaping () -> Void) { self.onNotFound = onNotFound }

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
    }
}
