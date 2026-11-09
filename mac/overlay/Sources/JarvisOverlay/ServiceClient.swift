import Foundation
import OverlayCore

enum ServiceError: Error {
    case noToken
    case badStatus(Int)
}

struct Health: Decodable {
    var ok: Bool
    /// Voice name when the service synthesizes audio itself; nil = speak locally.
    var voice: String?
}

final class ServiceClient {
    private let base: URL

    init() {
        let port = ProcessInfo.processInfo.environment["OVERLAY_PORT"].flatMap(Int.init) ?? 8791
        base = URL(string: "http://127.0.0.1:\(port)") ?? URL(string: "http://127.0.0.1:8791")!
    }

    func health() async throws -> Health {
        var request = try authorizedRequest(path: "/overlay/health", method: "GET")
        request.timeoutInterval = 2
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.check(response)
        return try JSONDecoder().decode(Health.self, from: data)
    }

    /// SSE event stream; yields each `event:` name (hello, wake, ...).
    func events() -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let request = try authorizedRequest(path: "/overlay/events", method: "GET")
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    try Self.check(response)
                    for try await line in bytes.lines {
                        guard line.hasPrefix("event:") else { continue }  // ": ping" etc. ignored
                        let name = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
                        if !name.isEmpty { continuation.yield(name) }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// NDJSON utterance stream; one decoded event per line.
    func utterance(text: String, convId: String, sttFinalMs: Double, screen: ScreenPayload?) -> AsyncThrowingStream<OverlayEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try authorizedRequest(path: "/overlay/utterance", method: "POST")
                    request.timeoutInterval = 90
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.httpBody = try JSONEncoder().encode(
                        Body(text: String(text.prefix(500)),
                             convId: String(convId.prefix(64)),
                             sttFinalMs: sttFinalMs,
                             screen: screen))
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    try Self.check(response)
                    for try await line in bytes.lines {
                        continuation.yield(OverlayEvent.decode(line: Data(line.utf8)))
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: - Internals

    private func authorizedRequest(path: String, method: String) throws -> URLRequest {
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.httpMethod = method
        // Token is read per request and never logged or printed.
        request.setValue("Bearer \(try Self.token())", forHTTPHeaderField: "Authorization")
        return request
    }

    private static func token() throws -> String {
        // The service writes the token file; OVERLAY_TOKEN_FILE overrides its location.
        let url: URL
        if let path = ProcessInfo.processInfo.environment["OVERLAY_TOKEN_FILE"], !path.isEmpty {
            url = URL(fileURLWithPath: path)
        } else {
            url = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/JarvisOverlay/token")
        }
        guard let raw = try? String(contentsOf: url, encoding: .utf8) else { throw ServiceError.noToken }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw ServiceError.noToken }
        return trimmed
    }

    private static func check(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw ServiceError.badStatus((response as? HTTPURLResponse)?.statusCode ?? -1)
        }
    }

    private struct Body: Encodable {
        var text: String
        var convId: String
        var sttFinalMs: Double
        var screen: ScreenPayload?
        private enum CodingKeys: String, CodingKey {
            case text, convId = "conv_id", sttFinalMs = "stt_final_ms", screen
        }
    }
}
