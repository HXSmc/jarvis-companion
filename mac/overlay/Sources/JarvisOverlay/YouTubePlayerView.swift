import SwiftUI
import WebKit
import OverlayCore

// Plays only through YouTube's official embedded player; never download, cache, re-host or
// modify the video or its thumbnail.
/// The embedded YouTube player inside a `PlayerHostView`: one id per view instance.
struct YouTubePlayerView: NSViewRepresentable {
    let id: String
    let key: String

    func makeCoordinator() -> Coordinator {
        Coordinator(key: key)
    }

    func makeNSView(context: Context) -> PlayerHostView {
        let host = PlayerHostView()
        guard MediaPolicy.isValidYouTubeID(id) else {
            MediaPlaybackController.shared.failed(key: key)
            return host
        }
        let coordinator = context.coordinator

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.preferences.isElementFullscreenEnabled = false
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let webView = WKWebView(frame: host.bounds, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = coordinator
        webView.uiDelegate = coordinator
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsMagnification = false
        host.addSubview(webView)

        var request = URLRequest(url: MediaPolicy.embedURL(id: id))
        request.setValue(MediaPolicy.embedReferer, forHTTPHeaderField: "Referer")
        webView.load(request)

        MediaPlaybackController.shared.attach(host, key: key)
        host.onTearDown = { [weak coordinator] in coordinator?.tearDown() }
        coordinator.startStartCheck(webView)
        return host
    }

    static func dismantleNSView(_ nsView: PlayerHostView, coordinator: Coordinator) {
        nsView.tearDown()
    }

    func updateNSView(_ nsView: PlayerHostView, context: Context) {}
}

extension YouTubePlayerView {
    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        private let key: String
        private var webView: WKWebView?
        private var startCheck: Task<Void, Never>?

        init(key: String) {
            self.key = key
        }

        /// "Did not start" guard: 8 s from load, the page must show a playing (or started)
        /// video element or the card fails (covers error 150/153, unavailable videos and a
        /// page that never finishes). Started in makeNSView so exactly one task exists per
        /// player, whether or not didFinish ever comes.
        /// playing = from click to teardown or error; read once here, no JS bridge.
        func startStartCheck(_ webView: WKWebView) {
            self.webView = webView
            let key = self.key
            startCheck = Task { [weak webView] in
                try? await Task.sleep(nanoseconds: 8_000_000_000)
                guard !Task.isCancelled, let webView else { return }
                let playing = (try? await webView.evaluateJavaScript(
                    "(function(){var v=document.querySelector('video');return !!v&&(v.currentTime>0||!v.paused);})()"
                )) as? Bool ?? false
                if !playing { MediaPlaybackController.shared.failed(key: key) }
            }
        }

        /// Idempotent; detach and blank the web view so media stops and nothing loads later.
        func tearDown() {
            guard let webView else { return }
            startCheck?.cancel()
            startCheck = nil
            self.webView = nil
            webView.pauseAllMediaPlayback(completionHandler: nil)
            webView.navigationDelegate = nil
            webView.uiDelegate = nil
            webView.loadHTMLString("", baseURL: nil)
            webView.stopLoading()
            webView.removeFromSuperview()
        }

        // MARK: WKNavigationDelegate

        /// No new windows, no downloads; sub-frames (googlevideo/ytimg) go free; the main
        /// frame may only stay on the embed page.
        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if navigationAction.shouldPerformDownload {
                decisionHandler(.cancel)
            } else if navigationAction.targetFrame == nil {
                decisionHandler(.cancel)
            } else if navigationAction.targetFrame?.isMainFrame == false {
                decisionHandler(.allow)
            } else if let url = navigationAction.request.url, MediaPolicy.isEmbedNavigation(url) {
                decisionHandler(.allow)
            } else {
                decisionHandler(.cancel)
            }
        }

        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationResponse: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            decisionHandler(navigationResponse.canShowMIMEType ? .allow : .cancel)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            MediaPlaybackController.shared.failed(key: key)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            MediaPlaybackController.shared.failed(key: key)
        }

        // MARK: WKUIDelegate

        /// No pop-ups, no "Watch on YouTube" window; the card's "Open in browser" is the only
        /// way out.
        func webView(_ webView: WKWebView,
                     createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction,
                     windowFeatures: WKWindowFeatures) -> WKWebView? {
            nil
        }

        func webView(_ webView: WKWebView,
                     requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedBy frame: WKFrameInfo,
                     type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(.deny)
        }
    }
}
