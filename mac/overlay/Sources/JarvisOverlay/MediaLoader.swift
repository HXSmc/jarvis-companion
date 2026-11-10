import Foundation
import ImageIO
import os

import OverlayCore

/// One finished download: the raw bytes (nil = refused or failed), the refusal reason
/// for the debug log, and the address the connection actually used.
private struct FetchResult: Sendable {
    var data: Data?
    var reason: String?
    var remoteAddress: String?
}

// @unchecked Sendable: mutable fetch state is confined to the session's serial delegate queue; the one continuation handover goes through the lock.
private final class FetchDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<FetchResult, Never>?
    private var buffer = Data()
    private var refusalReason: String?
    private var remoteAddress: String?
    private var redirectCount = 0

    func start(continuation: CheckedContinuation<FetchResult, Never>, task: URLSessionTask) {
        lock.lock()
        self.continuation = continuation
        lock.unlock()
        task.resume()
    }

    // MARK: URLSessionDataDelegate

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            refusalReason = "http-\((response as? HTTPURLResponse)?.statusCode ?? 0)"
            completionHandler(.cancel)
            return
        }
        guard MediaPolicy.isAllowedContentType(http.value(forHTTPHeaderField: "Content-Type")) else {
            refusalReason = "bad-type"
            completionHandler(.cancel)
            return
        }
        guard http.expectedContentLength <= Int64(ImageLimits.maxBytes) else {
            refusalReason = "too-large"
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        if buffer.count > ImageLimits.maxBytes {
            refusalReason = "too-large"
            buffer = Data()
            dataTask.cancel()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        redirectCount += 1
        guard let host = MediaPolicy.redirectHost(request.url, hop: redirectCount) else {
            completionHandler(nil)
            return
        }
        Task {
            let allowed = await MediaLoader.hostIsPublic(host)
            completionHandler(allowed ? request : nil)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didFinishCollecting metrics: URLSessionTaskMetrics) {
        remoteAddress = metrics.transactionMetrics.last?.remoteAddress
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        // server trust -> normal certificate validation; anything else would send credentials: refused
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust {
            completionHandler(.performDefaultHandling, nil)
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let refusalReason {
            finish(reason: refusalReason)
        } else if let error {
            finish(reason: (error as? URLError)?.code == .timedOut ? "timeout" : "failed")
        } else {
            finish(reason: nil)
        }
    }

    /// The single exit: resumes the one continuation exactly once, whatever ended the task.
    private func finish(reason: String?) {
        let data: Data? = reason == nil ? buffer : nil
        let result = FetchResult(data: data, reason: reason, remoteAddress: remoteAddress)
        lock.lock()
        let pending = continuation
        continuation = nil
        lock.unlock()
        pending?.resume(returning: result)
    }
}

/// Every image URL is untrusted (model output): https only, public addresses only (before
/// connecting, on every redirect, and the address actually connected to), 8 MB / 50 MP caps,
/// image types only, no cookies, no credentials, no disk.
@MainActor
final class MediaLoader {
    static let shared = MediaLoader()

    private static let log = Logger(subsystem: "JarvisOverlay", category: "MediaLoader")

    private let session: URLSession
    private let cache = NSCache<NSString, CGImage>()
    private var inFlight = 0
    private var waiters: [CheckedContinuation<Void, Never>] = []

    private init() {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.httpCookieAcceptPolicy = .never
        config.urlCredentialStorage = nil
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpAdditionalHeaders = ["User-Agent": "Mozilla/5.0 (Macintosh) OverlayMedia/1",
                                        "Accept": "image/*"]
        config.httpMaximumConnectionsPerHost = 2
        config.timeoutIntervalForRequest = ImageLimits.requestTimeout
        config.timeoutIntervalForResource = ImageLimits.resourceTimeout
        config.waitsForConnectivity = false
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        session = URLSession(configuration: config, delegate: nil, delegateQueue: queue)
        cache.totalCostLimit = ImageLimits.cacheCostLimit
        // Process-wide defense in depth, set before the first remote decode: ImageIO refuses
        // every other format (the magic-byte check in MediaPolicy runs first anyway).
        // (macOS 14.0-14.1 lack the call; the magic-byte check still holds there.)
        if #available(macOS 14.2, *) {
            _ = CGImageSourceSetAllowableTypes(Array(ImageLimits.allowedImageTypes) as CFArray)
        }
    }

    // MARK: public API

    /// The decoded, downsampled image, or nil on ANY refusal/failure. Memory cache only.
    func image(for urlString: String) async -> CGImage? {
        guard isSafeHTTPSURL(urlString), let url = URL(string: urlString),
              let host = url.host(percentEncoded: false) else { return nil }
        if let hit = cache.object(forKey: urlString as NSString) { return hit }
        // shortcut: no in-flight de-duplication, add one if the same URL is requested twice at once.
        await acquireSlot()
        defer { releaseSlot() }
        guard !Task.isCancelled else { return nil }
        guard await Self.hostIsPublic(host) else { return refuse(host, "host-refused") }
        let result = await fetch(url)
        if Task.isCancelled { return nil }
        guard let data = result.data else { return refuse(host, result.reason ?? "failed") }
        if let remote = result.remoteAddress {
            var literal = remote
            if let scope = literal.firstIndex(of: "%") { literal = String(literal[..<scope]) }
            guard let bytes = HostGuard.addressBytes(literal), HostGuard.isPublic(address: bytes) else {
                return refuse(host, "host-refused")
            }
        }
        // remoteAddress nil (metrics missing, e.g. through a proxy): the pre-connect host check stands.
        let decoded = await Task.detached(priority: .utility) { MediaPolicy.downsample(data: data) }.value
        guard let image = decoded else { return refuse(host, "failed") }
        cache.setObject(image, forKey: urlString as NSString, cost: image.width * image.height * 4)
        return image
    }

    /// HostGuard.isAllowedHostName(host) AND getaddrinfo (off the main thread) returns at least
    /// one address AND every returned address passes HostGuard.isPublic.
    nonisolated static func hostIsPublic(_ host: String) async -> Bool {
        guard HostGuard.isAllowedHostName(host) else { return false }
        return await Task.detached(priority: .utility) {
            var hints = addrinfo()
            hints.ai_family = AF_UNSPEC
            hints.ai_socktype = SOCK_STREAM
            var list: UnsafeMutablePointer<addrinfo>?
            guard getaddrinfo(host, nil, &hints, &list) == 0, let first = list else { return false }
            defer { freeaddrinfo(list) }
            var sawAddress = false
            var node: UnsafeMutablePointer<addrinfo>? = first
            while let current = node {
                if let bytes = MediaLoader.socketBytes(of: current.pointee) {
                    sawAddress = true
                    if !HostGuard.isPublic(address: bytes) { return false }
                }
                node = current.pointee.ai_next
            }
            return sawAddress
        }.value
    }

    // MARK: internals

    /// nil plus the one debug line every failed load may write (host and reason only, never the URL).
    private func refuse(_ host: String, _ reason: String) -> CGImage? {
        Self.log.debug("media refused host=\(host, privacy: .public) reason=\(reason, privacy: .public)")
        return nil
    }

    private func fetch(_ url: URL) async -> FetchResult {
        let delegate = FetchDelegate()
        let task = session.dataTask(with: URLRequest(url: url))
        task.delegate = delegate
        return await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                delegate.start(continuation: continuation, task: task)
            }
        } onCancel: {
            task.cancel()
        }
    }

    private func acquireSlot() async {
        if inFlight < ImageLimits.maxConcurrent {
            inFlight += 1
            return
        }
        await withCheckedContinuation { waiter in
            waiters.append(waiter)
        }
    }

    private func releaseSlot() {
        if let next = waiters.first {
            waiters.removeFirst()
            next.resume()  // the slot passes straight to the FIFO waiter; the count stays the same
        } else {
            inFlight -= 1
        }
    }

    /// The 4 or 16 address bytes of one resolved entry; nil for other address families.
    private nonisolated static func socketBytes(of info: addrinfo) -> [UInt8]? {
        guard let sa = info.ai_addr else { return nil }
        switch info.ai_family {
        case AF_INET:
            return withUnsafeBytes(of: sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                $0.pointee.sin_addr
            }) { Array($0) }
        case AF_INET6:
            return withUnsafeBytes(of: sa.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) {
                $0.pointee.sin6_addr
            }) { Array($0) }
        default:
            return nil
        }
    }
}
