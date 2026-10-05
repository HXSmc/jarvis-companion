import SwiftUI
import WebKit

/// Error surfaced when a gateway command arrives while the app is backgrounded —
/// DESIGN.md "Failure handling": JS is unreliable in background, so the app only
/// acts while foregrounded. `hostMismatch` is the HIGH-3 drop reason: a held
/// origin-guarded eval whose navigation finished on a different host.
enum JarvisError: Error {
    case backgrounded
    case screenshotFailed
    case missingField(String)
    case hostMismatch
}

/// An origin-guarded JS push (HIGH-3) waiting for the webview to finish a
/// navigation to `expectedHost` before it may run.
struct HeldEval {
    let js: String
    let expectedHost: String
    let completion: (Result<Any, Error>) -> Void
}

/// Owns the WKWebView the gateway drives (DESIGN.md "Components", ios row):
/// WS commands navigate it, evaluate JS on it (incl. the T5.2 credential-prefill
/// JS, which arrives as an evaluateJS payload carrying `expected_host`), and
/// screenshot it back.
final class WebController: NSObject, ObservableObject, WKNavigationDelegate {
    var webView: WKWebView?
    /// Set by the app from scenePhase; backgrounded commands fail fast instead
    /// of running unreliable background JS.
    var isForeground = true
    @Published var currentURL: URL?
    /// True after `didFinish` of the most recent navigation; reset by navigate.
    private var lastNavigationFinished = false
    /// An origin-guarded eval held until the expected host finishes loading.
    private var heldEval: HeldEval?

    func attach(_ view: WKWebView) {
        guard webView == nil else { return }
        webView = view
        view.navigationDelegate = self
    }

    func navigate(to url: URL) {
        currentURL = url
        lastNavigationFinished = false
        // A new navigation invalidates any eval still held for an earlier page.
        dropHeldEval()
        webView?.load(URLRequest(url: url))
    }

    /// Evaluate JS, optionally origin-bound (HIGH-3). With `expectedHost` the
    /// script is HELD until `didFinish` fires AND the webview's host equals
    /// `expectedHost`, then runs in the isolated `.defaultClient` content world
    /// (page scripts cannot see it — the prefill JS carries credentials).
    /// A navigation that finishes on any other host drops it with a
    /// hostMismatch error. Without `expectedHost` (plain evals) it runs
    /// immediately in the page world, as before.
    func evaluateJS(_ js: String, expectedHost: String?,
                    completion: @escaping (Result<Any, Error>) -> Void) {
        guard isForeground else {
            completion(.failure(JarvisError.backgrounded))
            return
        }
        guard let expectedHost = expectedHost else {
            webView?.evaluateJavaScript(js) { value, error in
                if let error = error {
                    completion(.failure(error))
                } else {
                    completion(.success(value ?? NSNull()))
                }
            }
            return
        }
        if lastNavigationFinished, webView?.url?.host == expectedHost {
            run(HeldEval(js: js, expectedHost: expectedHost, completion: completion))
            return
        }
        heldEval = HeldEval(js: js, expectedHost: expectedHost, completion: completion)
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

    /// Run a held eval now, in the `.defaultClient` content world.
    private func run(_ eval: HeldEval) {
        heldEval = nil
        webView?.evaluateJavaScript(eval.js, in: nil, in: .defaultClient) { result in
            switch result {
            case .success(let value):
                eval.completion(.success(value))
            case .failure(let error):
                eval.completion(.failure(error))
            }
        }
    }

    /// Drop the held eval (if any) with a hostMismatch failure.
    private func dropHeldEval() {
        guard let eval = heldEval else { return }
        heldEval = nil
        eval.completion(.failure(JarvisError.hostMismatch))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        lastNavigationFinished = true
        if let url = webView.url {
            DispatchQueue.main.async { self.currentURL = url }
        }
        // HIGH-3: release the held eval only when the finished navigation is on
        // the expected host; any other host drops it.
        if let eval = heldEval {
            if webView.url?.host == eval.expectedHost {
                run(eval)
            } else {
                dropHeldEval()
            }
        }
    }
}

/// SwiftUI wrapper exposing the shared WebController's WKWebView. The
/// controller owns the single instance for the app's whole lifetime: the first
/// host creates it via `attach`, and every later `makeUIView` (the pushed
/// fullScreenCover's host, or Home's hidden 1 pt host that re-claims it after
/// the cover dismisses) returns that SAME instance. Presenting or dismissing
/// never recreates or deallocates it, so gateway commands keep working while
/// the cover is closed (Phase 0 spec "Pushed page" lifetime rule).
struct JarvisWebView: UIViewRepresentable {
    let controller: WebController

    func makeUIView(context: Context) -> WKWebView {
        if let existing = controller.webView { return existing }
        let view = WKWebView()
        controller.attach(view)
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
