import SwiftUI
import WebKit
import OverlayCore

/// Marks a web card's map in the panel: OverlayPanel passes presses here straight through (pan),
/// while the card's title row still drags the card.
final class WebCardHostView: NSView {}

/// The sandboxed web card (jfollow10b ruling 3). Rules live in WebCardPolicy:
/// - pages and scripts only from the app bundle via the `jarvisweb` scheme handler; a compiled
///   block-all content rule list (only `jarvisweb://app/` passes) is in place before the first load;
/// - every response carries the CSP header (the page repeats it in a meta tag);
/// - the only main-frame load is the page's start URL, once; every later navigation, new window and
///   download is cancelled; non-persistent data store; no JS-to-native message handler;
/// - data.json is the decoded card; tiles come from the overlay service through ServiceClient, so
///   the gateway token never enters the web view.
struct WebCardView: NSViewRepresentable {
    let card: WebCard

    func makeCoordinator() -> Coordinator {
        Coordinator(card: card)
    }

    func makeNSView(context: Context) -> WebCardHostView {
        let host = WebCardHostView()
        let coordinator = context.coordinator
        guard let start = WebCardPolicy.startURL(page: card.page) else { return host }

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.setURLSchemeHandler(coordinator.handler, forURLScheme: WebCardPolicy.scheme)
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.preferences.isElementFullscreenEnabled = false
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true  // Leaflet
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        let webView = WKWebView(frame: host.bounds, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = coordinator
        webView.uiDelegate = coordinator
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsLinkPreview = false
        webView.allowsMagnification = false  // pinch zooms the map, not the page
        host.addSubview(webView)
        coordinator.webView = webView

        // No rules, no load: the page never runs without the block-all list in place.
        WKContentRuleListStore.default().compileContentRuleList(
            forIdentifier: "jarvis-web-card", encodedContentRuleList: WebCardPolicy.contentRules
        ) { [weak coordinator, weak webView] list, _ in
            Task { @MainActor in
                guard let coordinator, let webView else { return }
                guard let list else {
                    coordinator.report(rules: false, loaded: false)
                    return
                }
                webView.configuration.userContentController.add(list)
                coordinator.start = start
                webView.load(URLRequest(url: start))
            }
        }
        return host
    }

    static func dismantleNSView(_ nsView: WebCardHostView, coordinator: Coordinator) {
        coordinator.tearDown()
    }

    func updateNSView(_ nsView: WebCardHostView, context: Context) {}
}

extension WebCardView {
    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let handler: WebCardSchemeHandler
        weak var webView: WKWebView?
        var start: URL?
        private var startedLoad = false
        private var blocked = 0
        private var reported = false
        private let opened = Date()

        init(card: WebCard) {
            handler = WebCardSchemeHandler(card: card)
        }

        /// One `web_card` row per card: page, rules compiled, page loaded, ms, blocked navigations.
        func report(rules: Bool, loaded: Bool) {
            guard !reported else { return }
            reported = true
            let ms = Int(Date().timeIntervalSince(opened) * 1000)
            ServiceClient().debugNote(step: "web_card",
                                      detail: "page=map rules=\(rules ? 1 : 0) loaded=\(loaded ? 1 : 0) ms=\(ms) blocked=\(blocked)")
        }

        func tearDown() {
            handler.stopAll()
            guard let webView else { return }
            webView.navigationDelegate = nil
            webView.uiDelegate = nil
            webView.stopLoading()
            webView.removeFromSuperview()
            self.webView = nil
        }

        // MARK: WKNavigationDelegate

        /// Only the first main-frame load of the start URL; everything else (links, redirects,
        /// sub-frames, downloads) is cancelled.
        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if !navigationAction.shouldPerformDownload, !startedLoad,
               navigationAction.targetFrame?.isMainFrame == true,
               let start, navigationAction.request.url == start {
                startedLoad = true
                decisionHandler(.allow)
            } else {
                blocked += 1
                decisionHandler(.cancel)
            }
        }

        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationResponse: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            decisionHandler(navigationResponse.canShowMIMEType ? .allow : .cancel)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            report(rules: true, loaded: true)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            report(rules: true, loaded: false)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            report(rules: true, loaded: false)
        }

        // MARK: WKUIDelegate

        func webView(_ webView: WKWebView,
                     createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction,
                     windowFeatures: WKWindowFeatures) -> WKWebView? {
            nil
        }

        func webView(_ webView: WKWebView,
                     requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo,
                     type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(.deny)
        }
    }
}

/// Serves `jarvisweb://app/...`: the fixed resource table from Resources/web, `data.json` from the
/// decoded card, and `tiles/z/x/y.png` through ServiceClient. Anything else fails unanswered.
@MainActor
final class WebCardSchemeHandler: NSObject, WKURLSchemeHandler {
    private let data: Data
    private let client = ServiceClient()
    private var live = Set<ObjectIdentifier>()

    init(card: WebCard) {
        data = (try? JSONEncoder().encode(card.data)) ?? Data("{}".utf8)
    }

    func stopAll() {
        live.removeAll()
    }

    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        let key = ObjectIdentifier(urlSchemeTask as AnyObject)
        live.insert(key)
        guard let url = urlSchemeTask.request.url else { return fail(urlSchemeTask) }
        if WebCardPolicy.isData(url) {
            return respond(urlSchemeTask, url: url, body: data, mime: "application/json")
        }
        if let tile = WebCardPolicy.tilePath(for: url) {
            Task { @MainActor in
                let body = try? await client.tile(z: tile.z, x: tile.x, y: tile.y)
                guard live.contains(key) else { return }  // stopped meanwhile: never touch the task
                if let body {
                    respond(urlSchemeTask, url: url, body: body, mime: "image/png")
                } else {
                    fail(urlSchemeTask)
                }
            }
            return
        }
        guard let rel = WebCardPolicy.resourcePath(for: url), let entry = WebCardPolicy.resources[rel],
              let base = Bundle.main.resourceURL?.appendingPathComponent("web", isDirectory: true),
              let body = try? Data(contentsOf: base.appendingPathComponent(entry.file)) else {
            return fail(urlSchemeTask)
        }
        respond(urlSchemeTask, url: url, body: body, mime: entry.mime)
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {
        live.remove(ObjectIdentifier(urlSchemeTask as AnyObject))
    }

    private func respond(_ task: WKURLSchemeTask, url: URL, body: Data, mime: String) {
        guard live.remove(ObjectIdentifier(task as AnyObject)) != nil else { return }
        let headers = ["Content-Type": mime, "Content-Security-Policy": WebCardPolicy.csp,
                       "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"]
        guard let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1",
                                             headerFields: headers) else { return }
        task.didReceive(response)
        task.didReceive(body)
        task.didFinish()
    }

    private func fail(_ task: WKURLSchemeTask) {
        guard live.remove(ObjectIdentifier(task as AnyObject)) != nil else { return }
        task.didFailWithError(URLError(.fileDoesNotExist))
    }
}
