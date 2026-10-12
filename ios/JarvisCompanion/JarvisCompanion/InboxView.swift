import Foundation
import SwiftUI

/// One inbox item from `GET /inbox` — J1 writes the store, the gateway serves
/// it as-is (summaries/next_steps were generated once at ingest). Keys are the
/// store's snake_case; `account`/`created_at` are not shown and not decoded.
struct InboxItem: Identifiable, Equatable, Codable {
    let id: String
    let vendor: String
    let sender: String
    let subject: String
    let receivedAt: String
    let summary: String
    let nextSteps: [String]
    var read: Bool

    enum CodingKeys: String, CodingKey {
        case id, vendor, sender, subject, summary, read
        case receivedAt = "received_at"
        case nextSteps = "next_steps"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? ""
        vendor = try c.decodeIfPresent(String.self, forKey: .vendor) ?? ""
        sender = try c.decodeIfPresent(String.self, forKey: .sender) ?? ""
        subject = try c.decodeIfPresent(String.self, forKey: .subject) ?? ""
        receivedAt = try c.decodeIfPresent(String.self, forKey: .receivedAt) ?? ""
        summary = try c.decodeIfPresent(String.self, forKey: .summary) ?? ""
        nextSteps = try c.decodeIfPresent([String].self, forKey: .nextSteps) ?? []
        read = try c.decodeIfPresent(Bool.self, forKey: .read) ?? false
    }

    /// `HH:mm dd MMM` for the row/detail; falls back to the raw store string
    /// if the ISO8601Z timestamp doesn't parse.
    var receivedDisplay: String {
        let iso = ISO8601DateFormatter()
        guard let date = iso.date(from: receivedAt) else { return receivedAt }
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "HH:mm dd MMM"
        return fmt.string(from: date)
    }
}

/// Single shared model (owned by AppModel) so the Home badge and the Inbox
/// screen see the same state. Same HTTP pattern as AppModel.ask(): ios token
/// as bearer, 30 s timeout, network callbacks hop to main before touching
/// @Published state. The gateway returns items newest-first; unread-first
/// grouping happens here.
final class InboxModel: ObservableObject {
    @Published var items: [InboxItem] = []
    @Published var unread = 0

    var unreadItems: [InboxItem] { items.filter { !$0.read } }
    var readItems: [InboxItem] { items.filter { $0.read } }

    /// GET /inbox → items + unread. `onDone` (main thread) fires when the
    /// round-trip finishes either way — pull-to-refresh resumes on it.
    func refresh(_ onDone: (() -> Void)? = nil) {
        guard let url = Self.endpoint("/inbox") else {
            onDone?()
            return
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                defer { onDone?() }
                guard let self = self, error == nil,
                      let http = response as? HTTPURLResponse,
                      (200...299).contains(http.statusCode),
                      let data = data,
                      let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      let rawItems = obj["items"] as? [[String: Any]] else { return }
                let decoded = rawItems.compactMap { try? JSONSerialization.data(withJSONObject: $0) }
                    .compactMap { try? JSONDecoder().decode(InboxItem.self, from: $0) }
                self.items = decoded
                self.unread = obj["unread"] as? Int ?? decoded.filter { !$0.read }.count
            }
        }.resume()
    }

    /// POST /inbox/{id}/read — optimistic flip locally (badge moves at once),
    /// then the server's recounted unread wins.
    func markRead(_ id: String) {
        guard let index = items.firstIndex(where: { $0.id == id && !$0.read }) else { return }
        items[index].read = true
        unread = max(0, unread - 1)
        guard let url = Self.endpoint("/inbox/\(id)/read") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(AppSettings.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 30
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self = self, error == nil,
                      let http = response as? HTTPURLResponse,
                      (200...299).contains(http.statusCode),
                      let data = data,
                      let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      let recounted = obj["unread"] as? Int else {
                    self?.refresh()
                    return
                }
                self.unread = recounted
            }
        }.resume()
    }

    /// Same F3 builder shape as AppModel.gatewayEndpoint: https enforced,
    /// trailing slash trimmed, query/fragment dropped.
    private static func endpoint(_ suffix: String) -> URL? {
        guard var comps = URLComponents(
            string: AppSettings.gatewayURL.trimmingCharacters(in: .whitespaces)
        ), comps.scheme?.lowercased() == "https", comps.host != nil else {
            return nil
        }
        var path = comps.path
        while path.hasSuffix("/") { path.removeLast() }
        comps.path = path + suffix
        comps.query = nil
        comps.fragment = nil
        return comps.url
    }
}

/// Inbox sheet: unread section first (blue dot + bold vendor), read section
/// below; swipe a row or open detail to mark read; pull to refresh.
struct InboxScreen: View {
    @ObservedObject var model: InboxModel

    var body: some View {
        NavigationView {
            Group {
                if model.items.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "tray")
                            .font(.system(size: 32))
                            .foregroundColor(.jarvisMuted)
                        Text("No vendor mail yet")
                            .font(.body)
                            .foregroundColor(.jarvisMuted)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List {
                        if !model.unreadItems.isEmpty {
                            Section("Unread") {
                                ForEach(model.unreadItems) { item in
                                    row(item)
                                }
                            }
                        }
                        if !model.readItems.isEmpty {
                            Section("Read") {
                                ForEach(model.readItems) { item in
                                    row(item)
                                }
                            }
                        }
                    }
                    .refreshable {
                        await withCheckedContinuation { cont in
                            model.refresh { cont.resume() }
                        }
                    }
                }
            }
            .navigationTitle("Inbox")
            .navigationBarTitleDisplayMode(.inline)
        }
        .preferredColorScheme(.dark)
        .onAppear { model.refresh() }
    }

    @ViewBuilder
    private func row(_ item: InboxItem) -> some View {
        NavigationLink {
            InboxDetailView(model: model, item: item)
        } label: {
            HStack(alignment: .center, spacing: 10) {
                if !item.read {
                    Circle().fill(Color.blue).frame(width: 7, height: 7)
                } else {
                    Circle().fill(.clear).frame(width: 7, height: 7)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.vendor.isEmpty ? item.sender : item.vendor)
                        .font(.system(.body, design: .default, weight: item.read ? .regular : .semibold))
                        .foregroundColor(.jarvisText)
                        .lineLimit(1)
                    Text(item.subject)
                        .font(.subheadline)
                        .foregroundColor(.jarvisMuted)
                        .lineLimit(2)
                }
                Spacer(minLength: 8)
                Text(item.receivedDisplay)
                    .font(.caption)
                    .foregroundColor(.jarvisMuted)
            }
            .contentShape(Rectangle())
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            if !item.read {
                Button("Mark read") { model.markRead(item.id) }
                    .tint(.blue)
            }
        }
        .listRowBackground(Color.jarvisSurface)
    }
}

/// Detail: full sender/subject/timestamp, J1's ingest-time summary, and the
/// next_steps as a checklist-style list, plus a Mark read button.
private struct InboxDetailView: View {
    @ObservedObject var model: InboxModel
    let item: InboxItem

    var body: some View {
        ZStack {
            Color.jarvisBackground.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(item.subject)
                        .font(.title3.weight(.semibold))
                        .foregroundColor(.jarvisText)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.vendor.isEmpty ? item.sender : item.vendor)
                            .font(.subheadline)
                            .foregroundColor(.jarvisMuted)
                        Text(item.sender)
                            .font(.caption)
                            .foregroundColor(.jarvisMuted)
                        Text(item.receivedDisplay)
                            .font(.caption)
                            .foregroundColor(.jarvisMuted)
                    }
                    if !item.summary.isEmpty {
                        Text(item.summary)
                            .font(.body)
                            .foregroundColor(.jarvisText)
                            .textSelection(.enabled)
                    }
                    if !item.nextSteps.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Next steps")
                                .font(.subheadline.weight(.semibold))
                                .foregroundColor(.jarvisText)
                            ForEach(Array(item.nextSteps.enumerated()), id: \.offset) { index, step in
                                HStack(alignment: .top, spacing: 8) {
                                    Image(systemName: "circle")
                                        .font(.caption)
                                        .foregroundColor(.jarvisAccent)
                                    Text(step)
                                        .font(.subheadline)
                                        .foregroundColor(.jarvisText)
                                }
                            }
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
                    }
                    if !item.read {
                        Button {
                            model.markRead(item.id)
                        } label: {
                            Text("Mark read")
                                .font(.body.weight(.semibold))
                                .foregroundColor(.white)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 12)
                                .background(
                                    RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisAccent)
                                )
                        }
                    }
                }
                .padding(16)
            }
        }
        .navigationTitle("Inbox")
        .navigationBarTitleDisplayMode(.inline)
        .preferredColorScheme(.dark)
    }
}
