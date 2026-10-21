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
}
