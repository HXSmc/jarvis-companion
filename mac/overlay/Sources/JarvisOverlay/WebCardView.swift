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

        private let page: String

        private var pageLoaded = false

        init(card: WebCard) {
            handler = WebCardSchemeHandler(card: card)
            page = card.page  // a WebCardPolicy.pages key (decoding refuses any other)
            super.init()
            handler.onModelStatus = { [weak self] error, refused, bytes in
                self?.report(rules: true, loaded: self?.pageLoaded ?? false,
                             extra: " bytes=\(bytes) refused=\(refused) error=\(error)")
            }
        }

        /// One `web_card` row per card: page, rules compiled, page loaded, ms, blocked navigations.
        /// A model page's row waits for its status ping (bytes, refused URLs, error word).
        func report(rules: Bool, loaded: Bool, extra: String = "") {
            guard !reported else { return }
            reported = true
            let ms = Int(Date().timeIntervalSince(opened) * 1000)
            ServiceClient().debugNote(step: "web_card",
                                      detail: "page=\(page) rules=\(rules ? 1 : 0) loaded=\(loaded ? 1 : 0) ms=\(ms) blocked=\(blocked)\(extra)")
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
            pageLoaded = true
            if page != "model" { report(rules: true, loaded: true) }  // model: the status ping reports
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
/// decoded card, `tiles/z/x/y.png` through ServiceClient (map page only) and `drawing.svg`, the
/// drawing card's sanitised SVG as an image (drawing page only), and for a model card its own
/// `object.glb` / `preview.png` through ServiceClient, re-checked (acceptGLB/acceptPNG), plus the
/// page's one `model-status/<error>/<refused>` ping. Anything else fails unanswered.
@MainActor
final class WebCardSchemeHandler: NSObject, WKURLSchemeHandler {
    private let data: Data
    private let svg: Data?  // drawing page only
    private let isMap: Bool
    private let modelSlug: String?  // model page only: the card's own slug, never the page's words
    private var modelBytes = 0
    var onModelStatus: ((String, Int, Int) -> Void)?
    private let client = ServiceClient()
    private var live = Set<ObjectIdentifier>()

    init(card: WebCard) {
        isMap = card.page == "map"
        modelSlug = card.model?.slug
        if let model = card.model {
            data = (try? JSONEncoder().encode(model)) ?? Data("{}".utf8)
            svg = nil
        } else if let drawing = card.drawing {  // the page reads only the legend; the SVG goes as an image
            data = (try? JSONEncoder().encode(["legend": drawing.legend])) ?? Data("{}".utf8)
            svg = Data(drawing.svg.utf8)
        } else {
            data = (try? JSONEncoder().encode(card.data)) ?? Data("{}".utf8)
            svg = nil
        }
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
        if WebCardPolicy.resourcePath(for: url) == WebCardPolicy.drawingImagePath {
            guard let svg else { return fail(urlSchemeTask) }  // never a bundle read under this name
            return respond(urlSchemeTask, url: url, body: svg, mime: "image/svg+xml")
        }
        if let slug = modelSlug, let kind = WebCardPolicy.modelFileKind(for: url) {
            Task { @MainActor in
                let body = try? await client.modelFile(slug: slug, kind: kind)
                guard live.contains(key) else { return }
                if let body, kind == "glb" ? WebCardPolicy.acceptGLB(body) : WebCardPolicy.acceptPNG(body) {
                    if kind == "glb" { modelBytes = body.count }
                    respond(urlSchemeTask, url: url, body: body, mime: kind == "glb" ? "model/gltf-binary" : "image/png")
                } else {
                    fail(urlSchemeTask)
                }
            }
            return
        }
        if modelSlug != nil, let status = WebCardPolicy.modelStatus(for: url) {
            onModelStatus?(status.error, status.refused, modelBytes)
            return respond(urlSchemeTask, url: url, body: Data(), mime: "text/plain")
        }
        if isMap, let tile = WebCardPolicy.tilePath(for: url) {
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
