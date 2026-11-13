import Foundation
import XCTest
@testable import OverlayCore

final class TurnEndpointerTests: XCTestCase {
    /// Owner, 02:18:24: "Can you show me the last, uh" was ended by endOfSpeech after 0.7 s.
    func testTrailingOffWaitsLonger() {
        for text in ["Can you show me the last, uh", "Can you show me the last, huh", "the last, huh?", "show me the", "Open the", "What is two plus",
                     "and the,", "um"] {
            XCTAssertEqual(TurnEndpointer.silence(after: text), TurnEndpointer.extended, text)
        }
        for text in ["Can you show me the last debugging session", "What time is it",
                     "Tell me a joke.", "Open Safari"] {
            XCTAssertEqual(TurnEndpointer.silence(after: text), TurnEndpointer.fast, text)
        }
    }

    func testOneSecondPauseAfterFillerDoesNotEndTheTurn() {
        let t0 = Date()
        let cut = "Can you show me the last, uh"
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: cut, lastTextAt: t0, lastVoiceAt: t0,
                                                now: t0.addingTimeInterval(1.0)))
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: cut, lastTextAt: t0, lastVoiceAt: t0,
                                               now: t0.addingTimeInterval(2.8)))
        let done = "Can you show me the last debugging session"
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: done, lastTextAt: t0, lastVoiceAt: t0,
                                               now: t0.addingTimeInterval(0.75)))
    }

    /// jfollow4 E: a 20-word request with natural 0.6 s pauses was cut by the fast window. Partials
    /// trail the voice, so mid-pause the text and the voice flag both sit ~0.75 s old while the
    /// request is still growing; a long unpunctuated partial gets the extended window.
    func testLongRequestPausesDoNotEndTheTurnEarly() {
        let t0 = Date()
        let request = ("please show me the weather in paris tomorrow morning and then the news "
            + "for the day after that as well plus the time")
            .split(separator: " ").map(String.init)
        XCTAssertGreaterThanOrEqual(request.count, 20)
        var text = ""
        for w in request.indices {
            text = request[...w].joined(separator: " ")
            // Natural pauses inside the request, after words 10, 13, 16 and 19.
            if w + 1 >= 10, (w + 1 - 10) % 3 == 0, w < request.count - 1 {
                // Mid-pause, 0.75 s since the last partial and its voice: not the end.
                XCTAssertFalse(TurnEndpointer.shouldEnd(text: text,
                                                        lastTextAt: t0,
                                                        lastVoiceAt: t0,
                                                        now: t0.addingTimeInterval(0.75)),
                               "cut at word \(w + 1): \(text)")
            }
        }
        // The finished short command stays fast.
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: "Open Safari", lastTextAt: t0, lastVoiceAt: t0,
                                               now: t0.addingTimeInterval(0.75)))
        // The long request does end: after the last word, 2.7 s of quiet (or 4 s of stuck text).
        let last = t0.addingTimeInterval(Double(request.count) * 0.4)
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: text, lastTextAt: last,
                                                lastVoiceAt: last, now: last.addingTimeInterval(2.0)))
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: text, lastTextAt: last,
                                               lastVoiceAt: last, now: last.addingTimeInterval(2.8)))
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: text, lastTextAt: last,
                                               lastVoiceAt: last.addingTimeInterval(4),
                                               now: last.addingTimeInterval(4)))  // stuck text
    }

    func testStillVoicedKeepsListeningWhilePartialsLag() {
        let t0 = Date()
        // Text stalled for 1 s but the mic heard voice 0.2 s ago: the user is still talking.
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "Tell me a short", lastTextAt: t0,
                                                lastVoiceAt: t0.addingTimeInterval(0.8),
                                                now: t0.addingTimeInterval(1.0)))
        // Constant "voice" (a fan) never holds a turn open past 4 s of unchanged text.
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: "What time is it", lastTextAt: t0,
                                               lastVoiceAt: t0.addingTimeInterval(4),
                                               now: t0.addingTimeInterval(4)))
    }

    func testHeldLineWaitsForTheWindowToClose() {
        // Mac run 2, B4: "no box, the change is on line 3" came during the follow-up window and the
        // push dropped it. Held, it plays when the window closes; a new turn or 15 s drops it.
        let t0 = Date(timeIntervalSince1970: 1000)
        var held = HeldLine<String>()
        held.hold("say", at: t0)
        held.hold("audio", at: t0.addingTimeInterval(0.1))
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(8)), ["say", "audio"])
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(8)), [])  // said once
        held.hold("say", at: t0)
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(HeldLine<String>.maxAge + 1)), [])  // too old
        held.hold("say", at: t0)
        held.drop()  // the owner started a new turn
        XCTAssertEqual(held.release(at: t0.addingTimeInterval(1)), [])
        for i in 0..<10 { held.hold("e\(i)", at: t0) }
        XCTAssertEqual(held.release(at: t0).count, HeldLine<String>.maxEvents)
    }

    func testPushedLineIsNotHeldThroughAnOpenWindow() {
        // Mac run 4, B5 fold: "folded" came 3 s into the follow-up window and was held to its end,
        // heard >= 5.3 s late. An open window nobody spoke in is closed for the line instead.
        XCTAssertEqual(PushTiming.decide(orb: .followUp, audioBusy: false), .closeWindow)
        XCTAssertEqual(PushTiming.decide(orb: .idle, audioBusy: false), .now)
        XCTAssertEqual(PushTiming.decide(orb: .error, audioBusy: false), .now)
        // Never over a reply, the owner's words or a running turn.
        XCTAssertEqual(PushTiming.decide(orb: .speaking, audioBusy: true), .hold)
        XCTAssertEqual(PushTiming.decide(orb: .idle, audioBusy: true), .hold)
        XCTAssertEqual(PushTiming.decide(orb: .followUp, audioBusy: true), .hold)
        XCTAssertEqual(PushTiming.decide(orb: .listening, audioBusy: false), .hold)
        XCTAssertEqual(PushTiming.decide(orb: .thinking, audioBusy: false), .hold)
    }

    func testReplayNeverRepeatsADroppedTurnsAudio() {
        // Mac run 4, 32: room words "Right. I thought so. Exactly" were dropped (no reply, so no new
        // drain); the next window replayed them from the old drain: "Thought so. Exactly. Next change".
        let drain = Date(timeIntervalSince1970: 1000)
        let heard = drain.addingTimeInterval(4)
        let from = TurnEndpointer.replayFrom(drainedAt: drain, heardUntil: heard)
        XCTAssertEqual(from, heard)
        let room = heard.addingTimeInterval(-0.5), owner = heard.addingTimeInterval(0.6)
        XCTAssertEqual(TurnEndpointer.replayable(stamps: [room, owner], drainedAt: from), [false, true])
        // A spoken reply drains after the turn it answered: the drain still rules.
        let later = heard.addingTimeInterval(3)
        XCTAssertEqual(TurnEndpointer.replayFrom(drainedAt: later, heardUntil: heard), later)
        XCTAssertEqual(TurnEndpointer.replayFrom(drainedAt: nil, heardUntil: heard), heard)
        XCTAssertEqual(TurnEndpointer.replayFrom(drainedAt: drain, heardUntil: nil), drain)
        XCTAssertNil(TurnEndpointer.replayFrom(drainedAt: nil, heardUntil: nil))
    }

    func testStripWakeOneBreath() {
        XCTAssertEqual(TurnEndpointer.stripWake("Hey Jarvis next change"), "next change")
    }

    func testCompleteTextEndsDespiteRoomVoice() {
        let t0 = Date()
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "Highlight the changes.", lastTextAt: t0,
                                                 lastVoiceAt: t0.addingTimeInterval(1.4),
                                                 now: t0.addingTimeInterval(1.4)))
        XCTAssertTrue(TurnEndpointer.shouldEnd(text: "Highlight the changes.", lastTextAt: t0,
                                                lastVoiceAt: t0.addingTimeInterval(1.6),
                                                now: t0.addingTimeInterval(1.55)))
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "show me the last", lastTextAt: t0,
                                                 lastVoiceAt: t0.addingTimeInterval(1.6),
                                                 now: t0.addingTimeInterval(2.0)))
        // Empty text never ends via the complete-hold rule.
        XCTAssertFalse(TurnEndpointer.shouldEnd(text: "", lastTextAt: t0,
                                                 lastVoiceAt: t0.addingTimeInterval(1.9),
                                                 now: t0.addingTimeInterval(1.9)))
    }

    func testReplayStartsAfterTheReplyAndEcho() {
        let d = Date()
        XCTAssertEqual(TurnEndpointer.replayStart(drainedAt: d), d.addingTimeInterval(0.15))
        let stamps = [d.addingTimeInterval(-0.5), d, d.addingTimeInterval(0.1),
                      d.addingTimeInterval(0.15), d.addingTimeInterval(0.4)]
        XCTAssertEqual(TurnEndpointer.replayable(stamps: stamps, drainedAt: d),
                       [false, false, false, true, true])
        XCTAssertEqual(TurnEndpointer.replayable(stamps: [d], drainedAt: nil), [false])
    }

    /// jfollow4 D4: the owner's follow-up onset 0.2-1.0 s after the reply's drain must be replayed;
    /// only chunks stamped before drain + echoTail are the reply's echo.
    func testFollowUpOnsetKept() {
        let t = Date(timeIntervalSince1970: 2000)
        let stamps = (0...30).map { t.addingTimeInterval(Double($0) * 0.1) }  // ring stamps every 0.1 s
        let flags = TurnEndpointer.replayable(stamps: stamps, drainedAt: t)
        for (i, onset) in [0.2, 0.5, 1.0].enumerated() {
            XCTAssertTrue(flags[[2, 5, 10][i]], "onset \(onset) s after the drain must replay")
        }
        for (i, flag) in flags.enumerated() where Double(i) * 0.1 < TurnEndpointer.echoTail {
            XCTAssertFalse(flag, "chunk \(i * 100) ms after the drain is the reply's echo")
        }
    }

    /// Mac run r2, criterion 53 (0 of 3) and 41/44/47/48: requests were cut after ~8 words at
    /// the owner's pause ("Can you tell me when you have a moment", "...in a file called") while
    /// the owner went on talking. A long complete-looking partial no longer ends on 1.5 s of text
    /// silence while voice is still heard; it still ends on stuck text or on real quiet.
    func testLongRequestIsNotCutWhileTheOwnerStillTalks() {
        let t0 = Date()
        for text in ["Hey Jarvis, can you tell me, when you have a moment.",
                     "Can you tell me when you have a moment",
                     "Remember that when I say notes, I mean",
                     "Save these pages and overlays as our best"] {
            XCTAssertNil(TurnEndpointer.endRule(text: text, lastTextAt: t0,
                                                lastVoiceAt: t0.addingTimeInterval(1.5),
                                                now: t0.addingTimeInterval(1.6)), text)
            XCTAssertEqual(TurnEndpointer.endRule(text: text, lastTextAt: t0,
                                                  lastVoiceAt: t0.addingTimeInterval(4),
                                                  now: t0.addingTimeInterval(4)), "stuck", text)
        }
        // Real quiet after the last word still ends it fast.
        XCTAssertEqual(TurnEndpointer.endRule(text: "Can you tell me when you have a moment", lastTextAt: t0,
                                              lastVoiceAt: t0, now: t0.addingTimeInterval(0.75)), "fast")
        // A short command still ends on text silence over room voice.
        XCTAssertEqual(TurnEndpointer.endRule(text: "Hey Jarvis, highlight the changes.", lastTextAt: t0,
                                              lastVoiceAt: t0.addingTimeInterval(1.6),
                                              now: t0.addingTimeInterval(1.55)), "hold")
        // "called" / "named" trail off: the name is still coming.
        XCTAssertTrue(TurnEndpointer.looksIncomplete("Start a new session and say hello in a file called"))
        XCTAssertTrue(TurnEndpointer.looksIncomplete("stop the session named"))
        XCTAssertEqual(TurnEndpointer.endRule(text: "say hello in a file called", lastTextAt: t0,
                                              lastVoiceAt: t0, now: t0.addingTimeInterval(1.0)), nil)
    }

    /// Mac run r2: 100 ms mic buffers of one talker over a reverberant floor at a third of the
    /// speech level; the softer syllables (0.05) were not "voiced", so gaps over 0.7 s opened
    /// inside a sentence. Near the recent peak they count; the quiet room still does not.
    func testSofterSyllablesOfTheSameTalkerAreVoiced() {
        var vad = VoiceActivity()
        for _ in 0..<20 { _ = vad.isVoiced(rms: 0.03) }  // reverberant room between words
        XCTAssertTrue(vad.isVoiced(rms: 0.1))             // a loud syllable
        XCTAssertTrue(vad.isVoiced(rms: 0.05))            // a softer one, 0.5 of the peak
        XCTAssertFalse(vad.isVoiced(rms: 0.032))          // the room again: under the floor margin
        var quiet = VoiceActivity()
        for _ in 0..<100 { XCTAssertFalse(quiet.isVoiced(rms: 0.002)) }
        XCTAssertFalse(quiet.isVoiced(rms: 0.007))        // no peak: the floor margin rules
    }

    func testVoiceActivityTracksTheNoiseFloor() {
        var vad = VoiceActivity()
        for _ in 0..<100 { XCTAssertFalse(vad.isVoiced(rms: 0.002)) }  // quiet room
        XCTAssertTrue(vad.isVoiced(rms: 0.05))                          // speech
        for _ in 0..<50 { _ = vad.isVoiced(rms: 0.05) }
        XCTAssertTrue(vad.isVoiced(rms: 0.05))  // 1 s of speech does not become the floor
        XCTAssertFalse(vad.isVoiced(rms: 0.003))
    }

    // Round 6: a wake request keeps its words when quieter room talk follows (Mac runs 2-3).

    /// Feeds 100 ms blocks of `rms` from `start` for `seconds`.
    private func feed(_ gate: inout TailGate, _ rms: Double, from start: Date, seconds: Double) {
        var t = start
        while t < start.addingTimeInterval(seconds - 0.001) {
            t = t.addingTimeInterval(LevelBlocks.width)
            gate.level(rms, at: t)
        }
    }

    func testRoomWordsAfterACompleteRequestAreCut() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.12, from: t0, seconds: 1.5)                  // the owner: "Highlight the changes."
        gate.partial("Highlight the", at: t0.addingTimeInterval(1.0))
        gate.partial("Highlight the changes.", at: t0.addingTimeInterval(1.8))
        feed(&gate, 0.03, from: t0.addingTimeInterval(1.5), seconds: 3)  // the room, a quarter as loud
        gate.partial("Highlight the changes. What are you people", at: t0.addingTimeInterval(3.2))
        let text = "Highlight the changes. What are you people"
        XCTAssertNil(gate.cut(text: text, now: t0.addingTimeInterval(2.9)))  // owner quiet < 1.5 s
        XCTAssertEqual(gate.cut(text: text, now: t0.addingTimeInterval(3.05)), "Highlight the changes.")
    }

    /// jfollow4 E: room speech at 55% of the owner's peak glued its words onto his request under the
    /// old half-peak bar; only 0.6 of the peak still counts as him.
    func testRoomSpeechJustOverHalfHisPeakIsCut() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.12, from: t0, seconds: 1.5)                          // the owner's request
        gate.partial("Show me the last merge.", at: t0.addingTimeInterval(1.6))
        feed(&gate, 0.066, from: t0.addingTimeInterval(1.5), seconds: 3)   // the room at 0.55 of his peak
        gate.partial("Show me the last merge. And the one before", at: t0.addingTimeInterval(3.2))
        XCTAssertEqual(gate.cut(text: "Show me the last merge. And the one before",
                                now: t0.addingTimeInterval(3.05)), "Show me the last merge.")
        // He himself keeps the turn at 0.85 of his peak, still over the bar.
        var softer = TailGate()
        feed(&softer, 0.12, from: t0, seconds: 1.5)
        feed(&softer, 0.102, from: t0.addingTimeInterval(1.5), seconds: 1.0)  // trailing off
        softer.partial("Show me the last merge. Please.", at: t0.addingTimeInterval(2.9))
        XCTAssertEqual(softer.kept, "Show me the last merge. Please.")
    }

    func testOwnerContinuingAtHisLevelIsKept() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.12, from: t0, seconds: 1.5)
        gate.partial("Highlight the changes.", at: t0.addingTimeInterval(1.6))
        feed(&gate, 0.1, from: t0.addingTimeInterval(1.5), seconds: 1.0)  // he goes on, same distance
        gate.partial("Highlight the changes. And the next one.", at: t0.addingTimeInterval(2.9))
        XCTAssertEqual(gate.kept, "Highlight the changes. And the next one.")
        XCTAssertNil(gate.cut(text: "Highlight the changes. And the next one.", now: t0.addingTimeInterval(4.5)))
        // Late words of his own (STT lag under 1.2 s) are his, not the room's.
        var lagged = TailGate()
        feed(&lagged, 0.12, from: t0, seconds: 2.0)
        lagged.partial("show me the weather", at: t0.addingTimeInterval(1.2))
        lagged.partial("show me the weather in Paris", at: t0.addingTimeInterval(3.0))
        XCTAssertEqual(lagged.kept, "show me the weather in Paris")
        XCTAssertNil(lagged.cut(text: "show me the weather in Paris", now: t0.addingTimeInterval(5)))
    }

    // Round 8 (security): a quiet "no, wait" after "yes" must reach the service whole.
    func testARetractedYesIsNeverCut() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.10, from: t0, seconds: 0.4)                        // "Yes."
        gate.partial("Yes.", at: t0.addingTimeInterval(0.6))
        feed(&gate, 0.03, from: t0.addingTimeInterval(0.4), seconds: 1.5)  // "no, wait", muttered
        gate.partial("Yes. No, wait.", at: t0.addingTimeInterval(1.7))
        XCTAssertEqual(gate.kept, "Yes.")
        XCTAssertNil(gate.cut(text: "Yes. No, wait.", now: t0.addingTimeInterval(2.0)))
        // A bare yes is never the kept part, whatever follows.
        var bare = TailGate()
        feed(&bare, 0.10, from: t0, seconds: 0.4)
        bare.partial("Jarvis, yes.", at: t0.addingTimeInterval(0.6))
        feed(&bare, 0.03, from: t0.addingTimeInterval(0.4), seconds: 1.5)
        bare.partial("Jarvis, yes. What are you people", at: t0.addingTimeInterval(1.7))
        XCTAssertNil(bare.cut(text: "Jarvis, yes. What are you people", now: t0.addingTimeInterval(2.0)))
        // Retract words in the cut-off part keep a normal request whole too.
        var req = TailGate()
        feed(&req, 0.12, from: t0, seconds: 1.5)
        req.partial("Highlight the changes.", at: t0.addingTimeInterval(1.8))
        feed(&req, 0.03, from: t0.addingTimeInterval(1.5), seconds: 3)
        req.partial("Highlight the changes. Don't.", at: t0.addingTimeInterval(3.2))
        XCTAssertNil(req.cut(text: "Highlight the changes. Don't.", now: t0.addingTimeInterval(3.05)))
    }

    // Round 9 (security): a kept part that alone reads as a Confirm answer is never the cut.
    private func answerThenRoom(_ answer: String, _ room: String) -> String? {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.10, from: t0, seconds: 0.6)                          // the answer
        gate.partial(answer, at: t0.addingTimeInterval(0.8))
        feed(&gate, 0.03, from: t0.addingTimeInterval(0.6), seconds: 2.5)  // quieter, 1.5 s+ later
        let full = answer + " " + room
        gate.partial(full, at: t0.addingTimeInterval(2.4))
        XCTAssertEqual(gate.kept, answer)
        return gate.cut(text: full, now: t0.addingTimeInterval(2.6))
    }

    func testAPoliteYesIsNeverCut() {
        XCTAssertNil(answerThenRoom("Yes, thank you.", "actually only the docs"))
        XCTAssertNil(answerThenRoom("Confirmed, thanks.", "But just the readme"))
        XCTAssertNil(answerThenRoom("Jarvis, yes, thank you.", "actually only on the test branch"))
        XCTAssertNil(answerThenRoom("Hey, John Pierce. Yes, thanks.", "only the docs"))  // misheard wake
        XCTAssertNil(answerThenRoom("No thanks.", "well maybe the readme"))
        XCTAssertNil(answerThenRoom("Never mind, sir.", "just the docs then"))
        // A real request still gets its room words cut.
        XCTAssertEqual(answerThenRoom("Highlight the changes.", "What are you people"), "Highlight the changes.")
    }

    func testAnIncompleteRequestIsNeverCut() {
        let t0 = Date()
        var gate = TailGate()
        feed(&gate, 0.12, from: t0, seconds: 1.0)
        gate.partial("show me the last", at: t0.addingTimeInterval(1.1))
        feed(&gate, 0.03, from: t0.addingTimeInterval(1.0), seconds: 3)
        gate.partial("show me the last thing they said", at: t0.addingTimeInterval(3.5))
        XCTAssertNil(gate.kept)
        XCTAssertNil(gate.cut(text: "show me the last thing they said", now: t0.addingTimeInterval(4)))
        XCTAssertEqual(TailGate(ratio: 7).ratio, TailGate.defaultRatio)  // a bad service value is ignored
    }

    func testLevelBlocksAreRMSPer100ms() {
        let t0 = Date()
        let samples = [LevelSample(at: t0.addingTimeInterval(0.01), rms: 0.1, frames: 1024),
                       LevelSample(at: t0.addingTimeInterval(0.05), rms: 0.3, frames: 1024),
                       LevelSample(at: t0.addingTimeInterval(0.25), rms: 0.02, frames: 1024),
                       LevelSample(at: t0.addingTimeInterval(-0.5), rms: 0.9, frames: 1024)]  // before `from`
        let blocks = LevelBlocks.blocks(samples, from: t0, to: t0.addingTimeInterval(0.3))
        XCTAssertEqual(blocks.count, 3)
        XCTAssertEqual(blocks[0], 0.2236, accuracy: 0.0001)  // sqrt((0.01 + 0.09) / 2)
        XCTAssertEqual(blocks[1], 0)
        XCTAssertEqual(blocks[2], 0.02, accuracy: 0.0001)
        XCTAssertEqual(LevelBlocks.blocks(samples, from: t0, to: t0), [])
    }
}
