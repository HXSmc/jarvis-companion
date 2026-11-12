import AppKit
import Foundation
import OverlayCore

@MainActor
final class AppController {
    private let model = OverlayModel()
    private let client = ServiceClient()
    private let audio = AudioOut()
    private let speech = SpeechInput()
    private var overlayPanel: OverlayPanel?
    private var cardPanel: CardPanel?
    /// Live-test trigger; nil unless the app started with the layout debug flag.
    private var layoutDebug: LayoutDebug?

    // Conversation/turn state (latency log).
    private var convId = UUID().uuidString
    private var lastConvUse = Date.distantPast
    private var turnEndOfSpeech: Date?
    private var turnSttFinalMs: Double = 0
    private var turnConvId = ""
    private var turnFirstAudio = false
    private var turnDone = false
    /// Bumped by every wake; a follow-up window opens only if no new wake came meanwhile.
    private var listenSerial = 0
    private var heldLine = HeldLine<OverlayEvent>()
    /// When the last turn's speech was handed over: ring audio before it is never replayed again.
    private var lastHeardUntil: Date?

    // Barge-in / stop state.
    /// The running turn's utterance stream task; a barge-in or a stop cancels it so no later
    /// sentence of that reply is ever enqueued.
    private var utteranceTask: Task<Void, Never>?
    /// Bumped per turn; .audio/.say events of an older turn are ignored (barge-in, stop).
    private var turnToken = 0
    /// True while a turn's utterance stream is open; a wake during it barge-ins too.
    private var turnStreaming = false
    /// Barge-in wanted (health barge_in, default on).
    private var bargeInEnabled = true
    /// The barge listening run of the current turn, nil while its audio is not playing.
    private var bargeRun: BargeRun?
    /// The reply texts a wake cut (A4): stripped from that wake turn's text.
    private var cutReply: [String] = []
    /// Every .audio/.say text of the current turn so far, also the ones before the barge run
    /// exists (the first clip starts it): the run's reply is seeded from this.
    private var replyTexts: [String] = []
    /// When the current turn's first .audio arrived (the barge-in debug "at").
    private var bargeFirstAudioAt: Date?
    /// .audio events that arrived after their turn was cut (the barge-in dropped_sentences).
    private var lateAudioCount = 0
    /// end_listening: this turn opens no follow-up window (a stop closes the mic until the next wake).
    private var noFollowUp = false

    // Outage state.
    private var serviceVoice: String?
    private var outage = OutageGate(grace: 30)
    private var bannerTask: Task<Void, Never>?
    private var permissionTimer: Task<Void, Never>?

    /// Commit order of async shape/band adds (re-resolve, gutter OCR); `.clear` beats older ones.
    private var addOrder = ShapeTrack.AddOrder()
    private var addChain: Task<Void, Never>?
    private var lastRace: AddRace?
    /// Model keys of the last committed `lines` boxes per file: a newer `lines` for that file
    /// ("next change") replaces them. Keys only, the boxes live in the model.
    private var linesKeys: [String: [Int]] = [:]
    /// Gutter passes still retrying: a clear cancels them so no pass runs after it.
    private var linesWork: [Task<[ElementTrack.Resolved], Never>] = []

    func start() {
        let overlay = OverlayPanel()
        overlay.host(OverlayRootView(model: model))
        let cards = CardPanel(surface: overlay)
        cards.host(model)
        overlayPanel = overlay
        cardPanel = cards
        layoutDebug = LayoutDebug.startIfEnabled { [weak self] command, text in self?.runDebug(command, text) }

        audio.onLevel = { [weak self] level in self?.model.level = level }
        audio.onFirstAudio = { [weak self] in self?.markFirstAudio() }
        audio.onDrain = { [weak self] in self?.handleAudioDrained() }

        Task { await launch() }
    }

    // MARK: - Launch

    private func launch() async {
        await requestPermissionsIfNeeded()
        do {
            // AEC is probed before the engine starts, so health is read once here for it.
            if let health = try? await client.health() { speech.aecEnabled = health.bargeAec ?? false }
            try speech.start()
            if speech.aecEnabled && !speech.aecActive { aecFailed() }
        } catch {
            if speech.aecEnabled && !speech.aecActive { aecFailed() }
            model.orb = .error
            let line = "Microphone unavailable. Check System Settings > Privacy & Security > Microphone."
            model.banner = line
            audio.speakLocally(line)
        }
        await runEventsLoop()
    }

    /// The AEC probe did not stick: continue with AEC off (plain two-engine path, default margin).
    private func aecFailed() {
        speech.aecEnabled = false
        client.debugNote(step: "barge_aec", detail: "failed")
    }

    private func requestPermissionsIfNeeded() async {
        let missing = Permissions.missing()
        guard !missing.isEmpty else { return }
        for permission in missing {
            _ = await Permissions.request(permission)
        }
        refreshPermissions()
        guard !model.missingPermissions.isEmpty else { return }

        model.orb = .error
        speakPermissionNeed()
    }

    /// Keeps the permission card in sync; re-checks every 10 s while any are missing.
    private func refreshPermissions() {
        let missing = Permissions.missing()
        if missing != model.missingPermissions { model.missingPermissions = missing }
        if missing.isEmpty {
            permissionTimer?.cancel()
            permissionTimer = nil
        } else if permissionTimer == nil {
            permissionTimer = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 10_000_000_000)
                    guard !Task.isCancelled, let self else { return }
                    self.refreshPermissions()
                }
            }
        }
    }

    private func speakPermissionNeed() {
        let names = model.missingPermissions.map { Permissions.paneName($0) }.joined(separator: " and ")
        audio.speakLocally("I need \(names) permission. I've put the steps on screen.")
    }

    /// Banner that clears itself after `seconds`; the outage banner is sticky, not timed.
    private func showBanner(_ text: String, for seconds: Double) {
        bannerTask?.cancel()
        model.banner = text
        bannerTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.model.banner = nil
        }
    }

    // MARK: - Events loop (wake) and outage handling

    private func runEventsLoop() async {
        while true {
            do {
                let health = try await client.health()
                serviceVoice = health.voice
                speech.tailRatio = health.tailLevelRatio ?? TailGate.defaultRatio
                bargeInEnabled = health.bargeIn ?? true
                speech.aecEnabled = health.bargeAec ?? false  // takes effect on the next start()
                endOutage()
                for try await event in client.events() {
                    endOutage()
                    if event.name == "wake" {
                        await handleWake()
                    } else if event.name == "hud" {
                        handlePush(OverlayEvent.decode(line: Data(event.data.utf8)))
                    }
                }
                serviceUnreachable("event stream ended")
            } catch {
                serviceUnreachable("\(error)")
            }
            // Retry fast: a reinstall or a Mac wake drops the service for only a few seconds.
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }

    /// A `hud` event pushed after its turn ended (an expired Confirm, a "no box" line): card always;
    /// its line when idle, in an open follow-up window nobody spoke in (closed for it, reopened after),
    /// else held until the reply drains or the window closes (`PushTiming`, `HeldLine`).
    private func handlePush(_ event: OverlayEvent) {
        let sounds: Bool
        switch event {
        case .audio: sounds = true
        case .say: sounds = serviceVoice == nil
        default: sounds = false
        }
        if sounds {
            switch PushTiming.decide(orb: model.orb, audioBusy: audio.isBusy) {
            case .hold:
                heldLine.hold(event, at: Date())
                return
            case .closeWindow:
                listenSerial += 1  // the window's pending callbacks are stale
                speech.cancel()    // the mic must not take Jarvis's line as the owner's
                model.orb = .idle
                turnDone = true    // the line's drain reopens the follow-up window
            case .now:
                break
            }
        }
        switch event.pushed(idle: true) {
        case .card(let card)?:
            model.add(card: card)
        case .audio(_, let rate, let pcm)?:
            audio.enqueue(pcm: pcm, rate: rate)
        case .say(let text)?:
            if serviceVoice == nil { audio.speakLocally(text) }
        default:
            break
        }
    }

    /// Says the outage only after 30 s of failed retries (see OutageGate), and says what to do.
    private func serviceUnreachable(_ why: String) {
        if outage.since == nil { log("service unreachable (\(why)), retrying") }
        guard outage.failed(at: Date()) else { return }
        log("service still unreachable after \(Int(outage.grace)) s: \(why)")
        bannerTask?.cancel()
        bannerTask = nil
        audio.speakLocally("My service is not running. I've put the fix on screen.")
        model.orb = .error
        model.banner = "Jarvis service is not running. Fix: run scripts/install-mac-overlay.sh again. "
            + "Details: ~/Library/Logs/JarvisOverlay/service.log"
    }

    private func log(_ line: String) {
        FileHandle.standardError.write(Data("[overlay] \(Date()) \(line)\n".utf8))
    }

    private func endOutage() {
        if outage.since != nil && !outage.announced { log("service reachable again") }
        guard outage.reached() else { return }
        log("service reachable again, outage over")
        bannerTask?.cancel()
        bannerTask = nil
        model.banner = nil
        model.orb = .idle
    }

    /// Wake chime, resolved once on the first wake; nil = off or unknown name (no retry).
    private lazy var wakeSound: NSSound? = {
        guard let name = WakeChime.soundName(env: ProcessInfo.processInfo.environment) else { return nil }
        guard let sound = NSSound(named: name) else {
            log("wake sound '\(name)' not found, chime off")
            return nil
        }
        sound.volume = WakeChime.volume
        return sound
    }()

    /// followUp: the open-mic window after a reply (no wake word, same conversation);
    /// replayFrom: the reply's drain time, so only post-reply ring audio is replayed.
    private func handleWake(followUp: Bool = false, replayFrom: Date? = nil) async {
        listenSerial += 1
        turnDone = false  // a barge-in's stale drain must not start a follow-up
        if followUp {
            model.orb = .followUp
        } else {
            refreshPermissions()
            if !model.missingPermissions.isEmpty { speakPermissionNeed() }
            model.orb = .listening
            if audio.isBusy || turnStreaming {
                cutReply = replyTexts  // the ring this wake turn replays holds that reply's echo
                bargeIn(reason: .wake)  // A4: a wake during playback cuts the old reply's stream too
            } else {
                cutReply = []
                audio.stop()
            }
            noFollowUp = false  // an end_listening closes the mic only until the next wake
            if model.missingPermissions.isEmpty, let chime = wakeSound {
                chime.stop()  // replay from the start if the last one still rings
                chime.play()
            }
        }
        // Screen capture starts at wake; only awaited when an utterance lands.
        let screenTask = Task { await ScreenContext.capture() }
        let listenStart = Date()
        let echoOf = followUp ? [] : cutReply
        speech.listenAfterWake(
            followUp: followUp,
            replayFrom: replayFrom,
            onPartial: { [weak self] _ in
                // Speech in the follow-up window: show the normal listening orb.
                if followUp, self?.model.orb == .followUp { self?.model.orb = .listening }
            },
            onFinal: { [weak self] heardText, sttFinalMs, endOfSpeech, heard in
                let heardUntil = Date()  // every ring chunk before this went to this turn
                // A wake that cut a reply: drop that reply's echo and a misheard wake word.
                let text = echoOf.isEmpty ? heardText : BargeWords.wakeTurnText(final: heardText, reply: echoOf)
                Task { @MainActor in
                    self?.lastHeardUntil = heardUntil
                    guard !text.isEmpty else { self?.model.orb = .idle; return }  // only the reply's echo
                    self?.startUtterance(text: text, followUp: followUp, bargeIn: false,
                                         sttFinalMs: sttFinalMs,
                                         audioMs: endOfSpeech.timeIntervalSince(listenStart) * 1000,
                                         endOfSpeech: endOfSpeech, heard: heard, screenTask: screenTask)
                }
            },
            onNothing: { [weak self] why in
                Task { @MainActor in
                    guard let self else { return }
                    if followUp {  // nobody spoke after the reply: close the window quietly
                        if self.model.orb == .followUp {
                            self.model.orb = .idle
                            // A line pushed while the reply or this window ran: say it now.
                            for event in self.heldLine.release(at: Date()) { self.handlePush(event) }
                        }
                        return
                    }
                    self.client.debugNote(step: "no_speech", detail: why)
                    // With no mic there is no speech to catch; the permission line covers it.
                    if !self.model.missingPermissions.contains(.microphone) {
                        self.audio.speakLocally("I didn't catch that.")
                    }
                    self.model.orb = .idle
                }
            })
    }

    // MARK: - Utterance turn

    /// A final text becomes a service turn. A stop phrase silences the old reply and cancels
    /// its stream before the request (B3); every new turn replaces the previous stream.
    private func startUtterance(text: String,
                                followUp: Bool,
                                bargeIn: Bool,
                                sttFinalMs: Double,
                                audioMs: Double,
                                endOfSpeech: Date,
                                heard: SpeechInput.Heard,
                                screenTask: Task<ScreenPayload?, Never>) {
        if StopPhrase.matches(text) {
            audio.stop()
            utteranceTask?.cancel()
            turnStreaming = false
            turnDone = false  // the stop's stale drains must not start a follow-up
        }
        turnToken += 1
        let token = turnToken
        turnStreaming = true
        utteranceTask = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.handleUtterance(text: text,
                                       followUp: followUp,
                                       bargeIn: bargeIn,
                                       sttFinalMs: sttFinalMs,
                                       audioMs: audioMs,
                                       endOfSpeech: endOfSpeech,
                                       heard: heard,
                                       screenTask: screenTask,
                                       token: token)
            if self.turnToken == token { self.turnStreaming = false }
        }
    }

    private func handleUtterance(text: String,
                                 followUp: Bool,
                                 bargeIn: Bool,
                                 sttFinalMs: Double,
                                 audioMs: Double,
                                 endOfSpeech: Date,
                                 heard: SpeechInput.Heard,
                                 screenTask: Task<ScreenPayload?, Never>,
                                 token: Int) async {
        heldLine.drop()  // a new turn: an older pushed line is out of date
        model.orb = .thinking
        bargeRun = nil
        replyTexts = []  // the new turn's reply texts collect from scratch
        bargeFirstAudioAt = nil
        lateAudioCount = 0
        noFollowUp = false
        let screen = await screenTask.value

        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized == "clear" || normalized == "clear the screen" {
            clearAll()
        }

        let conv = conversationID()
        turnEndOfSpeech = endOfSpeech
        turnSttFinalMs = sttFinalMs
        turnConvId = conv
        turnFirstAudio = false
        turnDone = false

        let captured = ElementTrack.captured(screen)
        var gotEvent = false
        var failure: Error?
        for attempt in 1...2 {
            do {
                for try await event in client.utterance(text: text, convId: conv,
                                                        sttFinalMs: sttFinalMs, audioMs: audioMs,
                                                        stt: speech.lastDiagnosis, followUp: followUp,
                                                        bargeIn: bargeIn,
                                                        heard: heard, screen: screen) {
                    // A barge-in or a stop cut this turn: nothing more of it is drawn or heard.
                    guard !Task.isCancelled, token == turnToken else { return }
                    gotEvent = true
                    switch event {
                    case .overlay(let e): handle(e, captured: captured, token: token)
                    case .control(let c): handleControl(c)
                    }
                }
                return
            } catch {
                if Task.isCancelled || token != turnToken { return }  // cut on purpose, not an outage
                failure = error
                // Nothing played yet: the service may be mid-restart, so one retry is safe.
                guard attempt == 1, !gotEvent else { break }
                log("utterance request failed (\(error)), retrying once")
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        if let failure {
            // A 401/500/timeout never surfaces on the events loop; say it out loud.
            client.debugNote(step: "app_error", detail: "utterance request failed: \(failure)")
            audio.speakLocally("I couldn't reach my service. Try again.")
            showBanner("Couldn't reach Jarvis service", for: 5)
            model.orb = .error  // until the next wake sets .listening
        }
    }

    /// One barge-in path (A3): silence the reply, cut its stream, drop held lines, listen.
    /// stop_ms = the confirmation to audio.stop() returning; at = ms since the reply's first audio.
    private func bargeIn(reason: BargeReason) {
        let confirmedAt = Date()
        let at = bargeFirstAudioAt.map { Int(confirmedAt.timeIntervalSince($0) * 1000) } ?? 0
        let pending = audio.pendingBuffers
        let streamOpen = turnStreaming  // the model was still sending: later sentences never came
        audio.stop()  // silence first: the target is 300 ms from the confirmation
        let stopMs = Int(Date().timeIntervalSince(confirmedAt) * 1000)
        utteranceTask?.cancel()
        turnStreaming = false
        turnDone = false  // the stop's stale drains must not start a follow-up
        heldLine.drop()
        model.orb = .listening
        client.debugNote(step: "barge_in",
                         detail: "at=\(at) reason=\(reason.rawValue) "
                             + "dropped_sentences=\(pending + lateAudioCount) stop_ms=\(stopMs) "
                             + "stream_open=\(streamOpen ? 1 : 0)")
    }

    /// A2: with the first .audio of the turn playing, one recognition turn listens for a barge-in:
    /// no ring replay, never ends on silence, and while unconfirmed no final/nothing. On confirm it
    /// runs on as the owner's ordinary follow-up turn; at the reply's drain it is cancelled silently.
    private func startBargeListeningIfNeeded() {
        guard bargeInEnabled, bargeRun == nil, model.missingPermissions.isEmpty else { return }
        let audioOut = audio
        let run = BargeRun(enabled: true, aec: speech.aecActive, outLevels: { audioOut.outputLevels })
        run.onConfirmed = { [weak self] reason in self?.bargeIn(reason: reason) }
        // One note per run with the gate's numbers (live tuning: coupling, peak ratio, runs).
        run.onEnded = { [weak self] summary in self?.client.debugNote(step: "barge_gate", detail: summary) }
        run.reply = replyTexts
        bargeRun = run
        // Screen capture starts when the barge turn starts; only awaited when its utterance lands.
        let screenTask = Task { await ScreenContext.capture() }
        let listenStart = Date()
        speech.listenAfterWake(
            followUp: true,
            barge: run,
            onPartial: { _ in },
            onFinal: { [weak self] text, sttFinalMs, endOfSpeech, heard in
                Task { @MainActor in
                    guard let self else { return }
                    // Unconfirmed: never a turn (the reply plays on). Confirmed: the recogniser
                    // also heard the reply's lead; strip it, the rest is the owner's.
                    guard let owned = BargeWords.turnText(final: text, reply: run.reply,
                                                          confirmed: run.confirmed,
                                                          freshFrom: run.freshFrom) else {
                        if run.confirmed { self.model.orb = .idle }  // nothing left to call the owner's
                        return
                    }
                    self.lastHeardUntil = Date()
                    self.startUtterance(text: owned, followUp: true, bargeIn: true,
                                        sttFinalMs: sttFinalMs,
                                        audioMs: endOfSpeech.timeIntervalSince(listenStart) * 1000,
                                        endOfSpeech: endOfSpeech, heard: heard, screenTask: screenTask)
                }
            },
            onNothing: { _ in })
    }

    /// Control events steer the overlay itself: end_listening closes the follow-up window for
    /// this turn (B3), card_size draws one card enlarged or restores it (C3).
    private func handleControl(_ event: ControlEvent) {
        switch event {
        case .endListening:
            noFollowUp = true
        case .cardSize(let id, let max):
            guard model.cards.contains(where: { $0.id == id }) else { return }
            if max { _ = model.enlarge.max(id) } else { model.enlarge.restore() }
            model.activity()  // a restored card gets a fresh idle countdown, not the old one's end
        }
    }

    /// `captured`: the turn's capture items in points (ElementTrack.captured), to tell a grounded
    /// shape from coordinates the brain wrote itself. `token`: this turn's token; .audio/.say of
    /// an older turn (cut by a barge-in or a stop) never sound.
    private func handle(_ event: OverlayEvent, captured: [CGRect], token: Int) {
        if ShapeTrack.extendsIdle(event) { model.activity() }
        switch event {
        case .say(let text):
            guard token == turnToken else { return }
            replyTexts.append(text)
            bargeRun?.reply = replyTexts
            if serviceVoice == nil { audio.speakLocally(text) }
        case .audio(let text, let rate, let pcm):
            guard token == turnToken else { lateAudioCount += 1; return }
            if bargeFirstAudioAt == nil { bargeFirstAudioAt = Date() }
            replyTexts.append(text)
            bargeRun?.reply = replyTexts
            model.orb = .speaking
            audio.enqueue(pcm: pcm, rate: rate)
            startBargeListeningIfNeeded()  // seeded with replyTexts, this clip included
        case .shape(let shape, let check):
            // The capture is seconds old: re-resolve once, off the main actor, before drawing.
            let stale = shape.rect, label = shape.label ?? ""
            let role = check?.role, checkLabel = check?.label ?? ""
            // An AX pick or a capture item is grounded; a rect the brain wrote is drawn only where
            // its label is re-found.
            let grounded = role != nil || ShapeTrack.grounded(stale, in: captured)
            let work = Task.detached(priority: .userInitiated) { () -> [ElementTrack.Resolved] in
                if role == nil, grounded, let window = ElementTrack.windowShape(stale: stale) { return window }
                let found: ElementTrack.Found? = await ShapeTrack.reresolve(
                    hasCheck: role != nil,
                    recheck: {
                        role.flatMap { ScreenContext.recheck(role: $0, label: checkLabel, near: stale) }
                            .map { ElementTrack.Found(rect: $0.frame, element: $0.element) }
                    },
                    lookup: { ElementTrack.lookup(near: stale) },
                    refind: { await ElementTrack.refind(label: label, stale: stale, grounded: grounded) })
                return ElementTrack.anchor(stale: stale, label: label, found: found, grounded: grounded)
            }
            enqueueAdd(work, budget: 1.5, fallback: { ElementTrack.fallback(stale: stale, grounded: grounded) },
                       template: shape)
        case .card(let card):
            model.add(card: card)
        case .lines(let file, let ranges, let labels):
            // Every outcome posts one reason code to the turn's debug record (codes and numbers only).
            let note: @Sendable (String) -> Void = { detail in
                Task { @MainActor [weak self] in self?.client.debugNote(step: "lines", detail: detail) }
            }
            let work = Task.detached(priority: .utility) {
                await ElementTrack.resolveBands(ranges, labels: labels, file: file, report: note)
            }
            linesWork = Array(linesWork.suffix(7)) + [work]  // finished ones need no cancel; keep the list short
            // One box at a time: the earlier boxes of this file go now, while Jarvis says "Change 2 of 3",
            // not when the new pass commits up to 5 s later (add() still replaces any that commit meanwhile).
            if let old = linesKeys.removeValue(forKey: file) { model.remove(keys: old) }
            enqueueAdd(work, budget: 5, fallback: { [] },
                       template: HUDShape(kind: .rect, box: [0, 0, 0, 0], label: Self.bandLabel), note: note,
                       linesFile: file)
        case .clear:
            clearAll()
        case .dropCards(let ids):
            model.remove(cardIDs: ids)
        case .layout(let mode):
            model.setLayout(mode)
        case .done:
            if audio.isBusy { turnDone = true } else { replyFinished() }
        case .unknown:
            break
        }
    }

    /// Layout debug trigger (LayoutDebug): the same paths the service's events take.
    private func runDebug(_ command: LayoutDebugCommand, _ text: String) {
        log("layout debug: \(text)")
        switch command {
        case .aside: handle(.layout(.aside), captured: [], token: turnToken)
        case .restore: handle(.layout(.restore), captured: [], token: turnToken)
        case .clear: handle(.clear, captured: [], token: turnToken)
        case .demo(let count):
            for card in LayoutDebugCommand.demoCards(count: count) { handle(.card(card), captured: [], token: turnToken) }
        case .demoMedia:
            for card in LayoutDebugCommand.demoMediaCards() { handle(.card(card), captured: [], token: turnToken) }
        case .demoMediaBad:
            for card in LayoutDebugCommand.demoMediaBadCards() { handle(.card(card), captured: [], token: turnToken) }
        case .shape:
            let size = overlayPanel?.frame.size ?? .zero
            let box = [size.width / 2 - 150, size.height / 2 - 40, size.width / 2 + 150, size.height / 2 + 40]
            model.add(shape: HUDShape(kind: .rect, box: box.map { Double($0) }, label: "Sample"))
            model.activity()
        }
    }

    /// Clears at once; adds still resolving lose and are never drawn (ShapeTrack.AddOrder).
    private func clearAll() {
        addOrder.clear()
        for work in linesWork { work.cancel() }
        linesWork.removeAll()
        linesKeys.removeAll()
        model.clear()
    }

    /// Serial add path: items commit to the model strictly in arrival order (their re-resolve
    /// already runs, in parallel). An item waits for its result at most its own budget, and at
    /// most 1.5 s after a later item arrived; then its fallback commits and the real result,
    /// when it comes, upgrades it (applyLate). An item that loses to a `.clear` is dropped with
    /// its AX refs.
    /// `note`: told when a clear wins over this add ("cleared") or its budget ran out first
    /// ("late": the result still draws when it comes).
    private func enqueueAdd(_ work: Task<[ElementTrack.Resolved], Never>, budget seconds: Double,
                            fallback: @escaping @Sendable () -> [ElementTrack.Resolved], template: HUDShape,
                            note: ((String) -> Void)? = nil, linesFile: String? = nil) {
        let ticket = addOrder.begin()
        let race = AddRace(fallback: fallback)
        Task.detached {
            let items = await work.value
            race.finish(.done(items))
        }
        race.timeout(after: seconds)
        lastRace?.timeout(after: Self.maxWaitBehind)
        lastRace = race
        let previous = addChain
        addChain = Task { [weak self] in
            await previous?.value
            let outcome = await race.wait()
            guard let self else { return }
            switch outcome {
            case .done(let items):
                if self.commit(items, ticket: ticket, template: template, linesFile: linesFile) == nil { note?("cleared") }
            case .late(let items):
                note?("late")
                let keys = self.commit(items, ticket: ticket, template: template, linesFile: linesFile)
                Task { [weak self] in
                    let real = await work.value
                    guard let self else { return }
                    if keys == nil || !self.addOrder.survives(ticket) { note?("cleared") }
                    self.applyLate(real, replacing: keys, ticket: ticket, template: template, linesFile: linesFile)
                }
            }
        }
    }

    /// A later add never waits longer than this behind an earlier one.
    private static let maxWaitBehind: Double = 1.5

    /// Commits one add in order; its model keys, or nil when a clear won.
    private func commit(_ items: [ElementTrack.Resolved], ticket: ShapeTrack.AddOrder.Ticket,
                        template: HUDShape, linesFile: String? = nil) -> [Int]? {
        guard addOrder.commit(ticket) else {
            ElementTrack.release(items.compactMap(\.track))
            return nil
        }
        return add(items, template: template, linesFile: linesFile)
    }

    /// Label of a `lines` band when the service sent no per-hunk label (an older service).
    private static let bandLabel = "changed"

    /// `linesFile`: this add is a `lines` event's boxes for that file.
    private func add(_ items: [ElementTrack.Resolved], template: HUDShape, linesFile: String? = nil) -> [Int] {
        if let linesFile {
            // A newer `lines` for the same file ("next change") replaces its earlier boxes.
            if let old = linesKeys.removeValue(forKey: linesFile) { model.remove(keys: old) }
            // Cards came before the bands (the band waits for the editor scroll): move any card that
            // covers the changed lines off them.
            if !items.isEmpty { cardPanel?.moveCards(off: items.map(\.rect)) }
        }
        var keys: [Int] = []
        var tracks: [Int: TrackAnchor] = [:]
        for item in items {
            var shape = template
            shape.label = item.label ?? template.label
            shape.box = [Double(item.rect.minX), Double(item.rect.minY), Double(item.rect.maxX), Double(item.rect.maxY)]
            let key = model.add(shape: shape)
            keys.append(key)
            if let track = item.track { tracks[key] = track }
        }
        let gone = Set(model.anchor(tracks))
        ElementTrack.release(tracks.filter { gone.contains($0.key) }.map(\.value),
                             keeping: tracks.filter { !gone.contains($0.key) }.map(\.value))
        if let linesFile { linesKeys[linesFile] = keys }
        return keys
    }

    /// A re-resolve that finished after its fallback was committed: move/upgrade the drawn box,
    /// draw what had no box, or remove a box whose target is positively gone. Nothing after a clear.
    private func applyLate(_ real: [ElementTrack.Resolved], replacing keys: [Int]?,
                           ticket: ShapeTrack.AddOrder.Ticket, template: HUDShape, linesFile: String? = nil) {
        guard let keys, addOrder.survives(ticket) else {
            ElementTrack.release(real.compactMap(\.track))
            return
        }
        if keys.isEmpty {
            _ = add(real, template: template, linesFile: linesFile)
        } else if real.isEmpty {
            model.remove(keys: keys)
        } else if real.count == keys.count {
            for (key, item) in zip(keys, real) where !model.replace(key: key, rect: item.rect, anchor: item.track) {
                ElementTrack.release(item.track.map { [$0] } ?? [])
            }
        } else {
            ElementTrack.release(real.compactMap(\.track))
        }
    }

    // MARK: - Latency log

    /// Same conversation id for 5 minutes, then a fresh UUID.
    private func conversationID() -> String {
        let now = Date()
        if now.timeIntervalSince(lastConvUse) > 300 { convId = UUID().uuidString }
        lastConvUse = now
        return convId
    }

    private func markFirstAudio() {
        guard let endOfSpeech = turnEndOfSpeech, !turnFirstAudio else { return }
        turnFirstAudio = true
        writeLatencyLine(endOfSpeech: endOfSpeech, firstAudio: Date())
    }

    private func writeLatencyLine(endOfSpeech: Date, firstAudio: Date) {
        let iso = ISO8601DateFormatter().string(from: firstAudio)
        let eosMs = Int(firstAudio.timeIntervalSince(endOfSpeech) * 1000)
        let line = "\(iso) eos_to_first_audio_ms=\(eosMs) stt_final_ms=\(Int(turnSttFinalMs)) conv=\(turnConvId)\n"

        let files = FileManager.default
        let dir = files.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/JarvisOverlay")
        try? files.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("latency.log")
        if let handle = FileHandle(forWritingAtPath: url.path) {
            defer { try? handle.close() }
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
        } else {
            try? line.write(to: url, atomically: true, encoding: .utf8)
        }
    }

    private func handleAudioDrained() {
        guard turnDone else { return }
        turnDone = false
        replyFinished()
    }

    /// Reply over: keep the mic open ~8 s so the user can go on without "Jarvis" (also how a
    /// trailing-off request gets its rest). 300 ms first so the room echo of the reply dies out.
    private func replyFinished() {
        model.orb = .idle
        // A line pushed while the reply played: say it now; its drain comes back here for the window.
        let held = heldLine.release(at: Date())
        if !held.isEmpty {
            turnDone = true
            for event in held { handlePush(event) }
            if audio.isBusy { return }
            turnDone = false
        }
        let serial = listenSerial
        guard !noFollowUp else { return }  // end_listening: the mic stays closed until the next wake
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard let self, self.listenSerial == serial, self.model.orb == .idle, !self.audio.isBusy
            else { return }
            await self.handleWake(followUp: true,
                                  replayFrom: TurnEndpointer.replayFrom(drainedAt: self.audio.lastDrainAt,
                                                                        heardUntil: self.lastHeardUntil))
        }
    }
}

/// One add's outcome: its result, its own budget, or a cut by a later add, whichever is first.
/// `.late` carries the fallback; the real result may still upgrade it.
private final class AddRace: @unchecked Sendable {
    enum Outcome {
        case done([ElementTrack.Resolved])
        case late([ElementTrack.Resolved])
    }

    private let lock = NSLock()
    private var outcome: Outcome?
    private var waiter: CheckedContinuation<Outcome, Never>?
    private let fallback: @Sendable () -> [ElementTrack.Resolved]

    init(fallback: @escaping @Sendable () -> [ElementTrack.Resolved]) { self.fallback = fallback }

    func finish(_ value: Outcome) {
        lock.lock()
        guard outcome == nil else {
            lock.unlock()
            return
        }
        outcome = value
        let parked = waiter
        waiter = nil
        lock.unlock()
        parked?.resume(returning: value)
    }

    /// Commits the fallback after `seconds` unless the result came first.
    func timeout(after seconds: Double) {
        Task.detached { [self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            if !isFinished { finish(.late(fallback())) }
        }
    }

    func wait() async -> Outcome {
        await withCheckedContinuation { park($0) }
    }

    private var isFinished: Bool {
        lock.lock()
        defer { lock.unlock() }
        return outcome != nil
    }

    private func park(_ continuation: CheckedContinuation<Outcome, Never>) {
        lock.lock()
        if let outcome {
            lock.unlock()
            continuation.resume(returning: outcome)
        } else {
            waiter = continuation
            lock.unlock()
        }
    }
}
