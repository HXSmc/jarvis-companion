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
    /// TailGate ratio (OVERLAY_TAIL_LEVEL_RATIO); nil from an older service.
    var tailLevelRatio: Double?
    /// Barge-in wanted; nil from an older service (the app defaults to true).
    var bargeIn: Bool?
    /// AEC-backed barge-in gate wanted; nil from an older service (the app defaults to false).
    var bargeAec: Bool?
    private enum CodingKeys: String, CodingKey {
        case ok, voice, tailLevelRatio = "tail_level_ratio", bargeIn = "barge_in", bargeAec = "barge_aec"
    }
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

    /// SSE event stream; yields each event's name (hello, wake, hud, ...) with its data line.
    func events() -> AsyncThrowingStream<(name: String, data: String), Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let request = try authorizedRequest(path: "/overlay/events", method: "GET")
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    try Self.check(response)
                    var sse = SSELines()  // ": ping" etc. ignored
                    for try await line in bytes.lines {
                        if let event = sse.feed(line) { continuation.yield(event) }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// NDJSON utterance stream; one decoded turn event (overlay content or control) per line.
    func utterance(text: String, convId: String, sttFinalMs: Double, audioMs: Double,
                   stt: String, followUp: Bool = false, bargeIn: Bool = false,
                   heard: SpeechInput.Heard? = nil,
                   screen: ScreenPayload?) -> AsyncThrowingStream<TurnEvent, Error> {
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
                             audioMs: audioMs,
                             stt: String(stt.prefix(500)),
                             followup: followUp,
                             bargeIn: bargeIn,
                             levels: heard?.levels,
                             conf: heard?.conf,
                             screen: screen))
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    try Self.check(response)
                    for try await line in bytes.lines {
                        continuation.yield(TurnEvent.decode(line: Data(line.utf8)))
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Fire-and-forget step for the debug page (no speech heard, service unreachable).
    func debugNote(step: String, detail: String = "") {
        Task {
            guard var request = try? authorizedRequest(path: "/overlay/debug/note", method: "POST") else { return }
            request.timeoutInterval = 2
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONEncoder().encode(["step": step, "detail": String(detail.prefix(500))])
            _ = try? await URLSession.shared.data(for: request)
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
        var audioMs: Double
        var stt: String
        var followup: Bool
        var bargeIn: Bool
        var levels: [Double]?
        var conf: Double?
        var screen: ScreenPayload?
        private enum CodingKeys: String, CodingKey {
            case text, convId = "conv_id", sttFinalMs = "stt_final_ms", audioMs = "audio_ms", stt, followup,
                 bargeIn = "barge_in", levels, conf, screen
        }
    }

    /// Saved-workspace answer (C): POSTs the snapshot body to /overlay/workspace/<nonce>. The
    /// service times the nonce out itself, so a failure is one debug note and swallowed.
    func postWorkspace(nonce: String, body: Data) async {
        do {
            var request = try authorizedRequest(path: "/overlay/workspace/\(nonce)", method: "POST")
            request.timeoutInterval = 5
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
            let (_, response) = try await URLSession.shared.data(for: request)
            try Self.check(response)
        } catch {
            debugNote(step: "workspace", detail: "post_failed")
        }
    }
}
