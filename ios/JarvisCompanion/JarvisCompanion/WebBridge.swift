import Foundation
import Security

/// Keychain-backed storage for secrets (LOW-1): the ios token never sits in
/// UserDefaults. Items are device-only (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`),
/// so they do not sync to other devices and stay locked before first unlock.
enum Keychain {
    private static let service = "com.jarviscompanion.ios"

    static func read(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8) else { return nil }
        return value
    }

    static func write(_ value: String, account: String) {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let data = Data(value.utf8)
        if SecItemCopyMatching(base as CFDictionary, nil) == errSecSuccess {
            SecItemUpdate(base as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        } else {
            var add = base
            add[kSecValueData as String] = data
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            SecItemAdd(add as CFDictionary, nil)
        }
    }
}

/// Stored config for the thin client (entered once in the app's settings fields):
/// the gateway base URL (the tailscale-serve HTTPS URL, e.g.
/// `https://<machine>.<tailnet>.<tld>:8790`) and the phone-only ios token — the
/// same value the gateway reads from the Secrets vault (`## Jarvis Companion`,
/// `ios_token:` line). The token lives in the Keychain (LOW-1), never
/// UserDefaults and never logs.
enum AppSettings {
    private static let gatewayKey = "jarvis.gatewayURL"
    private static let legacyTokenKey = "jarvis.token"
    private static let tokenAccount = "ios_token"

    /// One-time LOW-1 migration: a token previously stored in UserDefaults
    /// (by older app builds) moves into the Keychain once, then the
    /// UserDefaults copy is deleted for good.
    static let migrateTokenOnce: Void = {
        let legacy = UserDefaults.standard.string(forKey: legacyTokenKey) ?? ""
        if !legacy.isEmpty && Keychain.read(account: tokenAccount) == nil {
            Keychain.write(legacy, account: tokenAccount)
        }
        UserDefaults.standard.removeObject(forKey: legacyTokenKey)
    }()

    static var gatewayURL: String {
        get { UserDefaults.standard.string(forKey: gatewayKey) ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: gatewayKey) }
    }

    static var token: String {
        get {
            _ = migrateTokenOnce
            return Keychain.read(account: tokenAccount) ?? ""
        }
        set {
            _ = migrateTokenOnce
            Keychain.write(newValue, account: tokenAccount)
        }
    }
}

/// WS client speaking the T1.2 gateway protocol for the `ios` role: connects to
/// `<gateway>/ws?role=ios` with the token in the `Authorization: Bearer`
/// header (MED-2 — a query-param token would land in gateway access logs),
/// sends JSON `{id, type, payload}` and receives `{id, ok, data}` replies plus
/// gateway-pushed commands in the same envelope (e.g. T5.1's `{id, type:
/// "navigate", payload: {url}}`). Reconnects with exponential backoff + jitter
/// (DESIGN.md "Failure handling", iPhone WS drops row); the background-audio
/// session (KeepAlive.swift) holds the socket alive while the app is
/// backgrounded.
///
/// Main-thread confined: connect/disconnect/send are called on the main thread;
/// network callbacks hop to main before touching state.
final class WebBridge: NSObject, ObservableObject {
    /// Handler for gateway-pushed commands: (id, type, payload).
    var onCommand: ((String, String, [String: Any]) -> Void)?

    @Published var connected = false
    @Published var lastError: String?

    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private var reconnectWorkItem: DispatchWorkItem?
    private var wantConnected = false
    private var attempts = 0
    private var nextID = 0
    private var pending: [String: (Bool, [String: Any]) -> Void] = [:]

    /// Build the `/ws?role=ios` URL; https/http flip to wss/ws. The token rides
    /// the Authorization header, not the query string.
    static func socketURL(gateway: String) -> URL? {
        guard var components = URLComponents(string: gateway) else { return nil }
        components.path = components.path + "/ws"
        var query = components.queryItems ?? []
        query.append(URLQueryItem(name: "role", value: "ios"))
        components.queryItems = query
        if components.scheme == "https" { components.scheme = "wss" }
        if components.scheme == "http" { components.scheme = "ws" }
        return components.url
    }

    func connect() {
        wantConnected = true
        guard task == nil else { return }
        let gateway = AppSettings.gatewayURL
        let token = AppSettings.token
        guard !gateway.isEmpty, !token.isEmpty else {
            lastError = "set the gateway URL and ios token in Settings"
            return
        }
        guard let url = Self.socketURL(gateway: gateway) else {
            lastError = "invalid gateway URL: \(gateway)"
            return
        }
        if session == nil {
            session = URLSession(configuration: .default)
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let newTask = session!.webSocketTask(with: request)
        task = newTask
        newTask.resume()
        // No open callback on URLSessionWebSocketTask: ping once to confirm.
        newTask.sendPing { [weak self] error in
            DispatchQueue.main.async {
                guard let self = self, self.task === newTask else { return }
                if let error = error {
                    self.lastError = "connect failed: \(error.localizedDescription)"
                    self.handleDrop()
                } else {
                    self.attempts = 0
                    self.connected = true
                    self.lastError = nil
                }
            }
        }
        receiveLoop(on: newTask)
    }

    func disconnect() {
        wantConnected = false
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        connected = false
        pending.removeAll()
    }

    /// Send `{id, type, payload}`; `onReply` fires with the `{id, ok, data}` answer.
    @discardableResult
    func send(type: String, payload: [String: Any],
              onReply: ((Bool, [String: Any]) -> Void)? = nil) -> String? {
        guard let task = task else {
            lastError = "not connected"
            return nil
        }
        nextID += 1
        let id = "ios-\(nextID)"
        if let onReply = onReply { pending[id] = onReply }
        let envelope: [String: Any] = ["id": id, "type": type, "payload": payload]
        guard let data = try? JSONSerialization.data(withJSONObject: envelope),
              let text = String(data: data, encoding: .utf8) else {
            lastError = "payload not JSON-serializable"
            pending[id] = nil
            return id
        }
        // Send a TEXT frame (.string): .data would send a BINARY frame, which
        // the gateway's receive() loop would still decode, but TEXT is the
        // envelope's natural wire form and what every other client sends.
        task.send(.string(text)) { [weak self] _ in
            DispatchQueue.main.async {
                if self?.task == nil { self?.handleDrop() }
            }
        }
        return id
    }

    private func receiveLoop(on task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure:
                DispatchQueue.main.async { self.handleDrop() }
            case .success(let message):
                switch message {
                case .string(let text):
                    DispatchQueue.main.async { self.handle(text: text) }
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        DispatchQueue.main.async { self.handle(text: text) }
                    }
                @unknown default:
                    break
                }
                if task === self.task {
                    self.receiveLoop(on: task)
                }
            }
        }
    }

    private func handle(text: String) {
        guard let data = text.data(using: .utf8),
              let dict = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return
        }
        // Reply to one of our sends: `{id, ok, data}`.
        if let id = dict["id"] as? String, dict["ok"] != nil,
           let onReply = pending.removeValue(forKey: id) {
            let ok = dict["ok"] as? Bool ?? false
            let replyData = dict["data"] as? [String: Any] ?? [:]
            onReply(ok, replyData)
            return
        }
        // Gateway-pushed command: `{id, type, payload}`.
        if let id = dict["id"] as? String, let type = dict["type"] as? String {
            let payload = dict["payload"] as? [String: Any] ?? [:]
            onCommand?(id, type, payload)
        }
    }

    private func handleDrop() {
        guard task != nil else { return }
        task = nil
        connected = false
        pending.removeAll()
        scheduleReconnect()
    }

    /// Exponential backoff with jitter: min(30, 2^attempts) seconds, ±30%.
    private func scheduleReconnect() {
        guard wantConnected, reconnectWorkItem == nil else { return }
        attempts += 1
        let delay = min(30.0, pow(2.0, Double(attempts))) * Double.random(in: 0.7...1.3)
        lastError = "disconnected; reconnecting in \(Int(delay))s"
        let work = DispatchWorkItem { [weak self] in
            self?.reconnectWorkItem = nil
            self?.connect()
        }
        reconnectWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }
}
