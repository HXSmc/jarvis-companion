import Combine
import SwiftUI

/// App entry + scene + WS lifecycle ownership: the AppModel glues the WebBridge
/// (gateway WS, role=ios) to the WebController (driven WKWebView) and routes
/// gateway-pushed commands.
@main
struct JarvisCompanionApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .onOpenURL { url in model.openHandoff(url) }
                .onAppear { model.start() }
                .onChange(of: scenePhase) { model.sceneChanged($0) }
        }
    }
}

/// Glue owning the WS lifecycle (WebBridge) and the driven web view
/// (WebController). Gateway-pushed commands (`navigate`, `evaluateJS`,
/// `screenshot`) act on the web view; results go back over WS as
/// `{id, type: "result", payload: {ok, data}}`.
final class AppModel: ObservableObject {
    let bridge = WebBridge()
    let web = WebController()

    @Published var statusText = "starting"
    @Published var gatewayURLField = AppSettings.gatewayURL {
        didSet { AppSettings.gatewayURL = gatewayURLField.trimmingCharacters(in: .whitespaces) }
    }
    @Published var tokenField = AppSettings.token {
        didSet { AppSettings.token = tokenField }
    }
    private var cancellables: Set<AnyCancellable> = []

    init() {
        bridge.$connected
            .combineLatest(bridge.$lastError)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] connected, error in
                self?.statusText = connected ? "connected" : (error ?? "disconnected")
            }
            .store(in: &cancellables)
    }

    func start() {
        bridge.onCommand = { [weak self] id, type, payload in
            self?.handleCommand(id: id, type: type, payload: payload)
        }
        // The audio session must start foregrounded; it then holds the WS in
        // the background (KeepAlive).
        KeepAlive.start()
        KeepAlive.requestNotificationPermission()
        bridge.connect()
    }

    /// DESIGN.md "Feature data flows" #2: a `jarvis://handoff/<id>` link
    /// arrival opens the app, which sends `handoff_open` over WS; the gateway
    /// replies `{id, ok, data: {url}}` and pushes `navigate` — the WKWebView
    /// loads the checkout URL and the Mac drives it from there.
    func openHandoff(_ url: URL) {
        guard url.scheme == "jarvis", url.host == "handoff",
              let id = url.pathComponents.dropFirst().first, !id.isEmpty else {
            statusText = "unrecognized jarvis:// link: \(url.absoluteString)"
            return
        }
        statusText = "handoff \(id): opening"
        bridge.send(type: "handoff_open", payload: ["id": id]) { [weak self] ok, data in
            guard let self = self else { return }
            if ok, let urlString = data["url"] as? String, let target = URL(string: urlString) {
                self.web.navigate(to: target)
                self.statusText = "handoff \(id): driving"
            } else {
                self.statusText = "handoff \(id): gateway declined (\(data))"
            }
        }
    }

    func sceneChanged(_ phase: ScenePhase) {
        // DESIGN.md "Failure handling": JS is unreliable in background, so the
        // app only acts while foregrounded.
        web.isForeground = phase == .active
    }

    private func handleCommand(id: String, type: String, payload: [String: Any]) {
        switch type {
        case "navigate":
            guard let urlString = payload["url"] as? String, let url = URL(string: urlString) else {
                reportResult(id: id, ok: false, data: ["error": "navigate: missing url"])
                return
            }
            if !web.isForeground { KeepAlive.notifyHandoff(id: id) }
            web.navigate(to: url)
            reportResult(id: id, ok: true, data: ["url": urlString])
        case "evaluateJS":
            // Covers the T5.2 credential-prefill JS: the gateway injects the
            // prefill script as a `js` payload whose `expected_host` (HIGH-3)
            // binds it to the sign-in origin — the WebController holds it until
            // that host finishes loading and runs it in the .defaultClient
            // world; any other host drops it with a bare "host mismatch" data.
            guard let js = payload["js"] as? String else {
                reportResult(id: id, ok: false, data: ["error": "evaluateJS: missing js"])
                return
            }
            let expectedHost = payload["expected_host"] as? String
            web.evaluateJS(js, expectedHost: expectedHost) { [weak self] result in
                switch result {
                case .success(let value):
                    self?.reportResult(id: id, ok: true, data: ["value": "\(value)"])
                case .failure(let error):
                    if case JarvisError.hostMismatch = error {
                        self?.reportResult(id: id, ok: false, data: "host mismatch")
                    } else {
                        self?.reportResult(id: id, ok: false, data: ["error": error.localizedDescription])
                    }
                }
            }
        case "screenshot":
            web.screenshot { [weak self] result in
                switch result {
                case .success(let base64):
                    self?.reportResult(id: id, ok: true, data: ["image": base64])
                case .failure(let error):
                    self?.reportResult(id: id, ok: false, data: ["error": error.localizedDescription])
                }
            }
        default:
            reportResult(id: id, ok: false, data: ["error": "unknown command: \(type)"])
        }
    }

    private func reportResult(id: String, ok: Bool, data: [String: Any]) {
        bridge.send(type: "result", payload: ["id": id, "ok": ok, "data": data])
    }
}

/// Thin UI: the driven WKWebView plus a settings drawer for the gateway URL and
/// bearer token (the stored config WebBridge reads).
struct ContentView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            JarvisWebView(controller: model.web)
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text(model.statusText).font(.footnote)
                TextField("Gateway URL (https://…:8790)", text: $model.gatewayURLField)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                SecureField("iOS token", text: $model.tokenField)
                    .textFieldStyle(.roundedBorder)
                Button("Reconnect") {
                    model.bridge.disconnect()
                    model.bridge.connect()
                }
                .font(.footnote)
            }
            .padding(8)
        }
    }
}
