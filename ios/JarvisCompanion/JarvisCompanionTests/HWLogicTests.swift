import ImageIO
import XCTest
@testable import JarvisCompanion

/// Hardware Mode phone logic with scripted, synthetic inputs (no camera, no network).
@MainActor
final class HWLogicTests: XCTestCase {

    // MARK: voice-in intents

    func testLocalCommandsEnglishAndArabic() {
        let cases: [(String, HWLocalCommand?)] = [
            ("repeat", .repeatLast), ("Say that again.", .repeatLast), ("عيد", .repeatLast),
            ("stop", .stop), ("Quiet", .stop), ("اسكت", .stop),
            ("light on", .torchOn), ("نور", .torchOn), ("torch off", .torchOff), ("طفي النور", .torchOff),
            ("where is the cable", nil), ("done", nil), ("خلصت", nil),  // these go to the brain
        ]
        for (text, want) in cases {
            XCTAssertEqual(HWLocalCommand(text), want, text)
        }
    }

    func testEchoFilterDropsJarvisOwnVoiceButNotTheUser() {
        let said = "Step 1 of 2: grip the cable plug at SATA1 and pull it straight out of the port."
        XCTAssertTrue(HWVoice.mostlyContained("grip the cable plug at SATA1", in: said))
        XCTAssertTrue(HWVoice.mostlyContained("pull it straight out of the port", in: said))
        // what the simulator's recognizer really returned for that line (numbers spelled out, a few misheard words)
        XCTAssertTrue(HWVoice.mostlyContained("Step one two grip the cable plug that sat one and pull it straight out of the court", in: said))
        XCTAssertTrue(HWVoice.mostlyContained("Step one of two grip the cable plug sat one and pull it straight out of the port", in: said))
        XCTAssertFalse(HWVoice.mostlyContained("where is the SATA2 port", in: said))
        XCTAssertFalse(HWVoice.mostlyContained("wait stop", in: said))
        XCTAssertFalse(HWVoice.mostlyContained("", in: said))
    }

    func testSpeechBeforeTheEngineRunsIsQueuedNotLost() {
        let voice = HWVoice()
        voice.speak("Step 1: unplug the cable.")
        let diag = voice.diagnostics()
        XCTAssertEqual((diag["counts"] as? [String: Int])?["speak"], 1)
        XCTAssertTrue((diag["events"] as? [String] ?? []).contains { $0.contains("speak queued") })
    }

    // MARK: pointer (overlay) mapping

    func testOverlayPixelsMapOntoTheImage() throws {
        let raw: [String: Any] = ["target_px": [300, 420, 420, 470], "action": "plug", "arrow_dir": [1, 0], "label": "SATA2"]
        let o = try XCTUnwrap(HWSession.parseOverlay(raw, width: 1024, height: 768, frameId: 3))
        XCTAssertEqual(o.rect.minX, 300.0 / 1024, accuracy: 1e-6)
        XCTAssertEqual(o.rect.minY, 420.0 / 768, accuracy: 1e-6)
        XCTAssertEqual(o.rect.width, 120.0 / 1024, accuracy: 1e-6)
        XCTAssertEqual(o.action, "plug")
        XCTAssertEqual(o.frameId, 3)
        // reversed corners and out-of-image boxes are normalized and clamped
        let r = try XCTUnwrap(HWSession.parseOverlay(["target_px": [1100, 800, 900, 700]], width: 1024, height: 768, frameId: nil))
        XCTAssertEqual(r.rect.maxX, 1, accuracy: 1e-6)
        XCTAssertNil(HWSession.parseOverlay(["target_px": [1, 2, 3]], width: 1024, height: 768, frameId: nil))
        XCTAssertNil(HWSession.parseOverlay(["target_px": [1, 2, 3, 4]], width: nil, height: nil, frameId: nil))
    }

    // MARK: scripted session replay (what the gateway sends)

    func testReplayShowsGatedStepsOverlayAndSpeech() {
        let hw = HWSession()
        var spoken: [String] = []
        var overlays: [HWOverlay?] = []
        hw.onSay = { spoken.append($0) }
        hw.onOverlay = { overlays.append($0) }
        func reply(_ d: [String: Any]?) {
            hw.handle(["type": "reply", "data": d ?? NSNull(), "frame_id": 1, "w": 1024, "h": 768, "total_s": 1.2])
        }
        hw.handle(["type": "ready", "model": "sonnet"])
        XCTAssertTrue(hw.connected)
        hw.handle(["type": "say", "text": "Step 1 of 2: pull the plug out of SATA1."])  // early speech
        reply(["step": 1, "of": 2, "say": "Step 1 of 2: pull the plug out of SATA1.",
               "overlay": ["target_px": [294, 290, 470, 360], "action": "pull", "arrow_dir": [1, 0], "label": "SATA1 plug"]])
        XCTAssertEqual(spoken, ["Step 1 of 2: pull the plug out of SATA1."], "early say must not be spoken twice")
        XCTAssertEqual(hw.step, 1)
        XCTAssertEqual(hw.total, 2)
        XCTAssertNotNil(hw.overlay)
        // a quiet watch check with nothing to say changes nothing
        hw.ask("[watch] ...", quiet: true)
        reply(["step": 1, "of": 2, "say": "", "overlay": NSNull()])
        XCTAssertEqual(hw.step, 1)
        XCTAssertNotNil(hw.overlay, "an all-good watch check must not wipe the pointer")
        // the gateway allowed the advance (done): the phone follows
        hw.ask("done")
        reply(["step": 2, "of": 2, "say": "Step 2: plug it into SATA2.",
               "overlay": ["target_px": [300, 420, 420, 470], "action": "plug", "arrow_dir": [-1, 0], "label": "SATA2"]])
        XCTAssertEqual(hw.step, 2)
        XCTAssertEqual(spoken.last, "Step 2: plug it into SATA2.")
        XCTAssertEqual(overlays.compactMap { $0?.label }, ["SATA1 plug", "SATA2"])
        // a reply the brain botched still speaks, so the user is never left in silence
        hw.ask("huh")
        reply(nil)
        XCTAssertEqual(spoken.last, "Sorry, I didn't get that. Ask me again.")
    }

    // MARK: the part's outline (gateway mask)

    func testMaskPNGBecomesATintedPartShape() throws {
        // a 4x2 gray mask like the gateway's: top row edge (255), bottom row inside (110), last column outside
        let w = 4, h = 2
        let ctx = try XCTUnwrap(CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w,
                                          space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue))
        let px = try XCTUnwrap(ctx.data).assumingMemoryBound(to: UInt8.self)
        for x in 0..<w {
            px[x] = x == 3 ? 0 : 255
            px[w + x] = x == 3 ? 0 : 110
        }
        let png = NSMutableData()
        let dest = try XCTUnwrap(CGImageDestinationCreateWithData(png as CFMutableData, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(dest, try XCTUnwrap(ctx.makeImage()), nil)
        XCTAssertTrue(CGImageDestinationFinalize(dest))
        let raw: [String: Any] = ["target_px": [10, 20, 50, 40], "label": "SATA cable",
                                  "mask_png": (png as Data).base64EncodedString()]
        let o = try XCTUnwrap(HWSession.parseOverlay(raw, width: 100, height: 100, frameId: 1))
        let mask = try XCTUnwrap(o.mask, "the mask did not decode")
        XCTAssertEqual(mask.width, 4)
        XCTAssertEqual(mask.height, 2)
        let pixels = try XCTUnwrap(mask.dataProvider?.data)
        withExtendedLifetime(pixels) {  // a raw pointer into the CFData: keep it alive while reading
            guard let b = CFDataGetBytePtr(pixels) else { return XCTFail("no pixel data") }
            let row = mask.bytesPerRow
            XCTAssertEqual(Int(b[3]), 255, accuracy: 3, "edge: solid")
            XCTAssertEqual(Int(b[row + 3]), 110, accuracy: 3, "inside: see-through")
            XCTAssertEqual(Int(b[3 * 4 + 3]), 0, "outside: clear")
            XCTAssertEqual(Int(b[0]), 255, accuracy: 3)  // yellow at full opacity
            XCTAssertEqual(Int(b[1]), 214, accuracy: 3)
        }
        // no mask, or a broken one: the plain box stays
        XCTAssertNil(try XCTUnwrap(HWSession.parseOverlay(["target_px": [10, 20, 50, 40]], width: 100, height: 100, frameId: 1)).mask)
        XCTAssertNil(try XCTUnwrap(HWSession.parseOverlay(["target_px": [10, 20, 50, 40], "mask_png": "bm90IGEgcG5n"],
                                                          width: 100, height: 100, frameId: 1)).mask)
    }

    // MARK: Jarvis's server voice

    func testServerVoiceIsPlayedAndThePhoneVoiceIsOnlyAFallback() async throws {
        let hw = HWSession()
        hw.audioWait = 0.2
        var spoken: [String] = [], played: [String] = []
        hw.onSay = { spoken.append($0) }
        hw.onAudio = { pcm, rate, text in
            XCTAssertEqual(pcm.count, 4)
            XCTAssertEqual(rate, 24000)
            played.append(text)
        }
        hw.handle(["type": "ready", "model": "sonnet", "voice": "bm_daniel"])
        let pcm = Data([1, 0, 2, 0]).base64EncodedString()
        // the line, then its audio: Jarvis's voice, the phone stays quiet
        hw.handle(["type": "say", "text": "Step 1 of 2: pull the plug."])
        hw.handle(["type": "audio", "text": "Step 1 of 2: pull the plug.", "rate": 24000, "pcm": pcm])
        XCTAssertEqual(played, ["Step 1 of 2: pull the plug."])
        XCTAssertEqual(hw.say, "Step 1 of 2: pull the plug.")
        // audio that comes before its line still plays when the line does
        hw.handle(["type": "audio", "text": "Good.", "rate": 24000, "pcm": pcm])
        hw.handle(["type": "say", "text": "Good."])
        XCTAssertEqual(played.last, "Good.")
        // Arabic is the phone's job at once
        hw.handle(["type": "say", "text": "اسحب الكابل"])
        XCTAssertEqual(spoken, ["اسحب الكابل"])
        // no audio in time: the phone reads it, and the late audio is not played on top
        hw.handle(["type": "say", "text": "Now plug it into SATA2."])
        try await Task.sleep(nanoseconds: 500_000_000)
        XCTAssertEqual(spoken.last, "Now plug it into SATA2.")
        hw.handle(["type": "audio", "text": "Now plug it into SATA2.", "rate": 24000, "pcm": pcm])
        XCTAssertEqual(played.count, 2)
        XCTAssertEqual(spoken.count, 2)
        // without a server voice every line is the phone's
        let plain = HWSession()
        var plainSpoken: [String] = []
        plain.onSay = { plainSpoken.append($0) }
        plain.handle(["type": "ready", "model": "sonnet", "voice": NSNull()])
        plain.handle(["type": "say", "text": "Hello."])
        XCTAssertEqual(plainSpoken, ["Hello."])
    }

    func testServerPCMBecomesHalfSecondFloatBuffers() {
        var samples: [Int16] = (0..<30000).map { Int16(truncatingIfNeeded: $0) }
        samples[0] = Int16.max
        samples[1] = Int16.min
        let data = samples.withUnsafeBufferPointer { Data(buffer: $0) }
        let bufs = HWVoice.pcmBuffers(data, rate: 24000)
        print("pcmBuffers: \(bufs.map { "\($0.frameLength)@\($0.format)" })")
        XCTAssertEqual(bufs.map { Int($0.frameLength) }, [12000, 12000, 6000])
        guard let first = bufs.first, first.floatChannelData != nil else { return XCTFail("no float buffer from pcmBuffers") }
        withExtendedLifetime(first) {  // a raw pointer into the buffer: the buffer must outlive the reads
            let ch = first.floatChannelData![0]
            XCTAssertEqual(ch[0], 32767.0 / 32768, accuracy: 1e-6)
            XCTAssertEqual(ch[1], -1, accuracy: 1e-6)
        }
        print("pcmBuffers: samples read")
        XCTAssertTrue(HWVoice.pcmBuffers(Data(), rate: 24000).isEmpty)
        print("pcmBuffers: empty data ok")
        XCTAssertTrue(HWSession.isArabic("خلصت"))
        XCTAssertFalse(HWSession.isArabic("SATA2, sir."))
    }

    // MARK: lost part -> locate

    func testLocateMovesTheBoxSilentlyAndNeverTheStep() {
        let hw = HWSession()
        var spoken: [String] = []
        hw.onSay = { spoken.append($0) }
        hw.handle(["type": "ready", "model": "sonnet"])
        hw.ask("plan")
        hw.handle(["type": "reply", "frame_id": 1, "w": 1024, "h": 768, "total_s": 1.0,
                   "data": ["step": 1, "of": 4, "say": "Step 1: unplug the power cable.",
                            "overlay": ["target_px": [300, 330, 350, 380], "action": "unplug", "label": "power cable"]]])
        XCTAssertEqual(spoken.count, 1)
        hw.locate("power cable")
        hw.handle(["type": "reply", "frame_id": 2, "w": 1024, "h": 768, "total_s": 1.0,
                   "data": ["step": 1, "of": 4, "say": "",
                            "overlay": ["target_px": [180, 420, 230, 470], "action": "unplug", "label": "power cable"]]])
        XCTAssertEqual(spoken.count, 1, "a found part needs no speech")
        XCTAssertEqual(hw.overlay?.rect.minX ?? 0, 180.0 / 1024, accuracy: 1e-6, "box moved to the new spot")
        XCTAssertEqual(hw.step, 1)
        // out of view: no box, and it says where to aim
        hw.locate("power cable")
        hw.handle(["type": "reply", "frame_id": 3, "w": 1024, "h": 768, "total_s": 1.0,
                   "data": ["step": 1, "of": 4, "say": "Point the camera at the back edge again.", "overlay": NSNull()]])
        XCTAssertNil(hw.overlay)
        XCTAssertEqual(spoken.last, "Point the camera at the back edge again.")
        // a botched background reply stays silent
        hw.locate("power cable")
        hw.handle(["type": "reply", "data": NSNull(), "total_s": 1.0])
        XCTAssertEqual(spoken.count, 2)
    }
}
