import AVFoundation
import Speech
import XCTest
@testable import JarvisCompanion

/// Voice out and voice in with synthetic audio only: every clip is made here by the
/// system TTS, then optionally made quiet or noisy. No recordings of anyone.
@MainActor
final class HWVoiceAudioTests: XCTestCase {

    /// The guidance lines of a scripted repair (what the brain says, step by step).
    static let stepLines = [
        "Step 1 of 2: grip the cable plug at SATA1 and pull it straight out of the port.",
        "SATA1 is free, now plug the cable end into the SATA2 port.",
        "Wrong port: the cable is on SATA3, so pull it off and move it up to SATA2.",
        "The cable is now seated in SATA2, so the move is complete.",
    ]

    // MARK: helpers

    /// AVSpeechSynthesizer must outlive its write() callbacks.
    private var synths: [AVSpeechSynthesizer] = []

    /// TTS -> PCM buffers through AVSpeechSynthesizer.write, the same call HWVoice uses.
    private func synthesize(_ text: String, language: String) async throws -> [AVAudioPCMBuffer] {
        guard let voice = AVSpeechSynthesisVoice(language: language) else {
            throw XCTSkip("no \(language) TTS voice on this simulator")
        }
        let synth = AVSpeechSynthesizer()
        synths.append(synth)
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = voice
        return try await withCheckedThrowingContinuation { cont in
            var buffers: [AVAudioPCMBuffer] = []
            var done = false
            synth.write(utterance) { buffer in
                let pcm = buffer as? AVAudioPCMBuffer
                DispatchQueue.main.async {  // all state on main: callbacks come on another queue
                    guard !done else { return }
                    guard let pcm, pcm.frameLength > 0 else {
                        done = true  // an empty buffer marks the end
                        cont.resume(returning: buffers)
                        return
                    }
                    buffers.append(pcm)
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
                if !done { done = true; cont.resume(returning: buffers) }
            }
        }
    }

    private func seconds(_ buffers: [AVAudioPCMBuffer]) -> Double {
        buffers.reduce(0) { $0 + Double($1.frameLength) / $1.format.sampleRate }
    }

    /// Same noise every run: a failure must be reproducible.
    private struct SeededNoise: RandomNumberGenerator {
        var state: UInt64
        mutating func next() -> UInt64 {
            state = state &* 6364136223846793005 &+ 1442695040888963407
            return state
        }
    }

    /// Player-format (22.05 kHz mono float) clip written to a temp file, with gain and noise.
    private func clip(_ buffers: [AVAudioPCMBuffer], gain: Float = 1, noise: Float = 0) throws -> URL {
        let voice = HWVoice()
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".caf")
        var file: AVAudioFile?
        var rng = SeededNoise(state: 42)
        var speech: Double = 0, hiss: Double = 0, n = 0
        for b in buffers {
            let converted = try XCTUnwrap(voice.convert(b), "convert to the player format failed")
            // convert() hands back the same buffer when it is already in the player format:
            // mix into a copy, or one variant's gain leaks into the next.
            let out = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: converted.format, frameCapacity: converted.frameLength))
            out.frameLength = converted.frameLength
            if let src = converted.floatChannelData?[0], let dst = out.floatChannelData?[0] {
                dst.update(from: src, count: Int(converted.frameLength))
            }
            if let ch = out.floatChannelData?[0] {
                for i in 0..<Int(out.frameLength) {
                    let v = ch[i] * gain, z = Float.random(in: -noise...noise, using: &rng)
                    speech += Double(v * v); hiss += Double(z * z); n += 1
                    ch[i] = v + z
                }
            }
            if file == nil { file = try AVAudioFile(forWriting: url, settings: out.format.settings) }
            try file?.write(from: out)
        }
        if noise > 0, hiss > 0 { print(String(format: "  clip SNR %.1f dB", 10 * log10(speech / hiss))) }
        return url
    }

    private func requireSpeechAuthorization() async throws {
        var status = SFSpeechRecognizer.authorizationStatus()
        if status == .notDetermined {
            status = await withCheckedContinuation { cont in
                var resumed = false
                SFSpeechRecognizer.requestAuthorization { s in
                    DispatchQueue.main.async { if !resumed { resumed = true; cont.resume(returning: s) } }
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 10) {  // a permission alert nobody taps
                    if !resumed { resumed = true; cont.resume(returning: .notDetermined) }
                }
            }
        }
        guard status == .authorized else {
            throw XCTSkip("speech recognition not authorized on this simulator (status \(status.rawValue))")
        }
    }

    /// Like the app, which starts listening again after a failed recognition: one retry.
    private func transcribe(_ url: URL, locale: String) async throws -> String {
        do {
            return try await transcribeOnce(url, locale: locale)
        } catch let error as NSError where error.domain == "kAFAssistantErrorDomain" || error.domain == "timeout" {
            print("STT retry after \(error.domain) \(error.code)")
            do {
                return try await transcribeOnce(url, locale: locale)
            } catch let again as NSError where again.domain == "timeout" {
                throw XCTSkip("speech recognizer gave no result in 30 s, twice (CI network)")
            }
        }
    }

    private func transcribeOnce(_ url: URL, locale: String) async throws -> String {
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale)), recognizer.isAvailable else {
            throw XCTSkip("speech recognizer \(locale) unavailable on this simulator")
        }
        let request = SFSpeechURLRecognitionRequest(url: url)
        request.shouldReportPartialResults = false  // like the app, server or on-device
        request.contextualStrings = HWVoice.commandHints  // the app's own recognizer hints
        return try await withCheckedThrowingContinuation { cont in
            var finished = false
            recognizer.recognitionTask(with: request) { result, error in
                let text = result?.isFinal == true ? result?.bestTranscription.formattedString : nil
                DispatchQueue.main.async {
                    guard !finished else { return }
                    if let text {
                        finished = true
                        cont.resume(returning: text)
                    } else if let error {
                        finished = true
                        cont.resume(throwing: error)
                    }
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
                if !finished { finished = true; cont.resume(throwing: NSError(domain: "timeout", code: 30)) }
            }
        }
    }

    // MARK: (b) voice out

    func testEveryStepLineProducesAudio() async throws {
        let voice = HWVoice()
        for line in Self.stepLines {
            let buffers = try await synthesize(line, language: "en-US")
            let secs = seconds(buffers)
            print("TTS \(String(format: "%.2f", secs))s  \(line)")
            XCTAssertGreaterThan(secs, 1.0, "no or too little audio for: \(line)")
            // and the audio survives the conversion to the playback format HWVoice schedules
            let converted = buffers.compactMap { voice.convert($0) }
            XCTAssertEqual(converted.count, buffers.count)
            XCTAssertEqual(converted.first?.format.sampleRate, 22050)
            XCTAssertGreaterThan(converted.reduce(0) { $0 + Int($1.frameLength) }, 22050)
        }
    }

    func testArabicGuidanceProducesAudio() async throws {
        let buffers = try await synthesize("الخطوة الأولى: افصل الكيبل من منفذ SATA1.", language: "ar-SA")
        print("TTS ar-SA \(String(format: "%.2f", seconds(buffers)))s")
        XCTAssertGreaterThan(seconds(buffers), 1.0)
    }

    // MARK: (a) voice in: TTS commands -> the app's recognizer (SFSpeech) -> intent

    func testSpokenCommandsAreRecognizedCleanQuietAndNoisy() async throws {
        try await requireSpeechAuthorization()
        let commands: [(String, [String], HWLocalCommand?)] = [
            ("Where is the cable?", ["cable"], nil),
            ("Done.", ["done"], nil),
            ("Repeat.", ["repeat"], .repeatLast),
            ("Stop.", ["stop"], .stop),
            ("Light on.", ["light"], .torchOn),
        ]
        // clean, quiet (far from the phone), noisy (~20 dB SNR, a workshop): must be heard right.
        let variants: [(String, Float, Float)] = [("clean", 1, 0), ("quiet", 0.15, 0), ("noisy", 1, 0.015)]
        for (text, words, intent) in commands {
            let buffers = try await synthesize(text, language: "en-US")
            for (name, gain, noise) in variants {
                let heard = try await transcribe(try clip(buffers, gain: gain, noise: noise), locale: "en-US")
                print("STT \(name): \(text) -> \(heard)")
                for w in words {
                    XCTAssertTrue(heard.lowercased().contains(w), "\(name): \(text) heard as \(heard)")
                }
                if let intent { XCTAssertEqual(HWLocalCommand(heard), intent, "\(name): \(heard)") }
            }
        }
    }

    /// Very loud noise (~10 dB SNR): Apple's recognizer may mishear or give up, but it must fail
    /// safe: never invent a "done" (that would skip a step) or a different local command.
    func testVeryNoisyCommandsFailSafe() async throws {
        try await requireSpeechAuthorization()
        let commands: [(String, HWLocalCommand?)] = [("Where is the cable?", nil), ("Repeat.", .repeatLast), ("Stop.", .stop)]
        for (text, intent) in commands {
            let buffers = try await synthesize(text, language: "en-US")
            let heard: String
            do {
                heard = try await transcribe(try clip(buffers, noise: 0.05), locale: "en-US")
            } catch let error as NSError where error.domain == "kAFAssistantErrorDomain" {
                print("STT very noisy: \(text) -> not recognized (\(error.code)): nothing is sent, safe")
                continue
            }
            print("STT very noisy: \(text) -> \(heard)")
            XCTAssertFalse(heard.lowercased().split(separator: " ").contains("done"), "invented a done: \(heard)")
            if let got = HWLocalCommand(heard) { XCTAssertEqual(got, intent, "wrong local command from \(heard)") }
        }
    }

    func testArabicSpokenDoneIsRecognized() async throws {
        try await requireSpeechAuthorization()
        let buffers = try await synthesize("خلصت", language: "ar-SA")
        let heard = try await transcribe(try clip(buffers), locale: "ar-SA")
        print("STT ar-SA: خلصت -> \(heard)")
        XCTAssertTrue(heard.contains("خلص"), heard)
    }

    func testJarvisOwnVoiceIsTreatedAsEcho() async throws {
        try await requireSpeechAuthorization()
        let line = Self.stepLines[0]
        let heard = try await transcribe(try clip(try await synthesize(line, language: "en-US"), gain: 0.4, noise: 0.02),
                                         locale: "en-US")
        print("STT echo: \(heard)")
        XCTAssertTrue(HWVoice.mostlyContained(heard, in: line), "echo not caught: \(heard)")
    }
}
