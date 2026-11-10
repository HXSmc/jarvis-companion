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
        webView.underPageBackgroundColor = .black
        host.addSubview(webView)

        // Teardown is wired before attach, so a stale view (its card no longer playing) is torn
        // down through the normal path and never loads.
        host.onTearDown = { [weak coordinator] in coordinator?.tearDown() }
        coordinator.startStartCheck(webView)
        MediaPlaybackController.shared.attach(host, key: key)
        guard MediaPlaybackController.shared.playingKey == key else { return host }

        var request = URLRequest(url: MediaPolicy.embedURL(id: id))
        request.setValue(MediaPolicy.embedReferer, forHTTPHeaderField: "Referer")
        webView.load(request)
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

        /// Start check: every 0.5 s for at most 8 s, ask the page once whether its video element
        /// has moved past 0 s. Yes -> the card drops its "Starting" cover; still no at 8 s -> the
        /// card fails with "Open in browser" (covers error 150/153, unavailable videos and a page
        /// that never loads). One task per player, cancelled in tearDown; no JS bridge.
        func startStartCheck(_ webView: WKWebView) {
            self.webView = webView
            let key = self.key
            startCheck = Task { [weak webView] in
                for _ in 0..<16 {
                    try? await Task.sleep(nanoseconds: 500_000_000)
                    guard !Task.isCancelled, let webView else { return }
                    let moving = (try? await webView.evaluateJavaScript(
                        "(function(){var v=document.querySelector('video');return !!v&&v.currentTime>0;})()"
                    )) as? Bool ?? false
                    if moving {
                        MediaPlaybackController.shared.markStarted(key: key)
                        return
                    }
                }
                guard !Task.isCancelled else { return }
                MediaPlaybackController.shared.failed(key: key)
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
                     initiatedByFrame frame: WKFrameInfo,
                     type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(.deny)
        }
    }
}
