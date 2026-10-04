import SwiftUI
import WebKit

/// Error surfaced when a gateway command arrives while the app is backgrounded —
/// DESIGN.md "Failure handling": JS is unreliable in background, so the app only
/// acts while foregrounded.
enum JarvisError: Error {
    case backgrounded
    case screenshotFailed
    case missingField(String)
}

/// Owns the WKWebView the gateway drives (DESIGN.md "Components", ios row):
/// WS commands navigate it, evaluate JS on it (incl. the T5.2 credential-prefill
/// JS, which arrives as an ordinary evaluateJS payload), and screenshot it back.
final class WebController: NSObject, ObservableObject, WKNavigationDelegate {
    var webView: WKWebView?
    /// Set by the app from scenePhase; backgrounded commands fail fast instead
    /// of running unreliable background JS.
    var isForeground = true
    @Published var currentURL: URL?

    func attach(_ view: WKWebView) {
        guard webView == nil else { return }
        webView = view
        view.navigationDelegate = self
    }

    func navigate(to url: URL) {
        currentURL = url
        webView?.load(URLRequest(url: url))
    }

    func evaluateJS(_ js: String, completion: @escaping (Result<Any, Error>) -> Void) {
        guard isForeground else {
            completion(.failure(JarvisError.backgrounded))
            return
        }
        webView?.evaluateJavaScript(js) { value, error in
            if let error = error {
                completion(.failure(error))
            } else {
                completion(.success(value ?? NSNull()))
            }
        }
    }

    /// PNG snapshot of the current page, base64-encoded for the WS reply envelope.
    func screenshot(completion: @escaping (Result<String, Error>) -> Void) {
        guard isForeground else {
            completion(.failure(JarvisError.backgrounded))
            return
        }
        webView?.takeSnapshot(with: nil) { image, error in
            if let error = error {
                completion(.failure(error))
            } else if let png = image?.pngData() {
                completion(.success(png.base64EncodedString()))
            } else {
                completion(.failure(JarvisError.screenshotFailed))
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let url = webView.url {
            DispatchQueue.main.async { self.currentURL = url }
        }
    }
}

/// SwiftUI wrapper exposing the shared WebController's WKWebView.
struct JarvisWebView: UIViewRepresentable {
    let controller: WebController

    func makeUIView(context: Context) -> WKWebView {
        let view = WKWebView()
        controller.attach(view)
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
