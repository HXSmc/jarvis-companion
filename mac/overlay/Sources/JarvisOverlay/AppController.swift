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

    // Outage state.
    private var serviceVoice: String?
    private var outage = OutageGate(grace: 30)
    private var bannerTask: Task<Void, Never>?
    private var permissionTimer: Task<Void, Never>?

    /// Commit order of async shape/band adds (re-resolve, gutter OCR); `.clear` beats older ones.
    private var addOrder = ShapeTrack.AddOrder()
    private var addChain: Task<Void, Never>?
    private var lastRace: AddRace?

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
            try speech.start()
        } catch {
            model.orb = .error
            let line = "Microphone unavailable. Check System Settings > Privacy & Security > Microphone."
            model.banner = line
            audio.speakLocally(line)
        }
        await runEventsLoop()
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
                endOutage()
                for try await name in client.events() {
                    endOutage()
                    if name == "wake" { await handleWake() }
                }
                serviceUnreachable("event stream ended")
            } catch {
                serviceUnreachable("\(error)")
            }
            // Retry fast: a reinstall or a Mac wake drops the service for only a few seconds.
            try? await Task.sleep(nanoseconds: 1_000_000_000)
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

    /// followUp: the open-mic window after a reply (no wake word, same conversation).
    private func handleWake(followUp: Bool = false) async {
        listenSerial += 1
        turnDone = false  // a barge-in's stale drain must not start a follow-up
        if followUp {
            model.orb = .followUp
        } else {
            refreshPermissions()
            if !model.missingPermissions.isEmpty { speakPermissionNeed() }
            model.orb = .listening
            audio.stop()  // barge-in
            if model.missingPermissions.isEmpty, let chime = wakeSound {
                chime.stop()  // replay from the start if the last one still rings
                chime.play()
            }
        }
        // Screen capture starts at wake; only awaited when an utterance lands.
        let screenTask = Task { await ScreenContext.capture() }
        let listenStart = Date()
        speech.listenAfterWake(
            followUp: followUp,
            onPartial: { [weak self] _ in
                // Speech in the follow-up window: show the normal listening orb.
                if followUp, self?.model.orb == .followUp { self?.model.orb = .listening }
            },
            onFinal: { [weak self] text, sttFinalMs, endOfSpeech in
                Task { @MainActor in
                    await self?.handleUtterance(text: text,
                                          followUp: followUp,
                                          sttFinalMs: sttFinalMs,
                                          audioMs: endOfSpeech.timeIntervalSince(listenStart) * 1000,
                                          endOfSpeech: endOfSpeech,
                                          screenTask: screenTask)
                }
            },
            onNothing: { [weak self] why in
                Task { @MainActor in
                    guard let self else { return }
                    if followUp {  // nobody spoke after the reply: close the window quietly
                        if self.model.orb == .followUp { self.model.orb = .idle }
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

    private func handleUtterance(text: String,
                                 followUp: Bool,
                                 sttFinalMs: Double,
                                 audioMs: Double,
                                 endOfSpeech: Date,
                                 screenTask: Task<ScreenPayload?, Never>) async {
        model.orb = .thinking
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
                                                        screen: screen) {
                    gotEvent = true
                    handle(event, captured: captured)
                }
                return
            } catch {
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

    /// `captured`: the turn's capture items in points (ElementTrack.captured), to tell a grounded
    /// shape from coordinates the brain wrote itself.
    private func handle(_ event: OverlayEvent, captured: [CGRect]) {
        if ShapeTrack.extendsIdle(event) { model.activity() }
        switch event {
        case .say(let text):
            if serviceVoice == nil { audio.speakLocally(text) }
        case .audio(_, let rate, let pcm):
            model.orb = .speaking
            audio.enqueue(pcm: pcm, rate: rate)
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
        case .lines(_, let ranges):
            enqueueAdd(Task.detached(priority: .utility) { await ElementTrack.resolveBands(ranges) },
                       budget: 5, fallback: { [] },
                       template: HUDShape(kind: .rect, box: [0, 0, 0, 0], label: "changed"))
        case .clear:
            clearAll()
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
        case .aside: handle(.layout(.aside), captured: [])
        case .restore: handle(.layout(.restore), captured: [])
        case .clear: handle(.clear, captured: [])
        case .demo(let count):
            for card in LayoutDebugCommand.demoCards(count: count) { handle(.card(card), captured: []) }
        case .demoMedia:
            for card in LayoutDebugCommand.demoMediaCards() { handle(.card(card), captured: []) }
        case .demoMediaBad:
            for card in LayoutDebugCommand.demoMediaBadCards() { handle(.card(card), captured: []) }
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
        model.clear()
    }

    /// Serial add path: items commit to the model strictly in arrival order (their re-resolve
    /// already runs, in parallel). An item waits for its result at most its own budget, and at
    /// most 1.5 s after a later item arrived; then its fallback commits and the real result,
    /// when it comes, upgrades it (applyLate). An item that loses to a `.clear` is dropped with
    /// its AX refs.
    private func enqueueAdd(_ work: Task<[ElementTrack.Resolved], Never>, budget seconds: Double,
                            fallback: @escaping @Sendable () -> [ElementTrack.Resolved], template: HUDShape) {
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
                _ = self.commit(items, ticket: ticket, template: template)
            case .late(let items):
                let keys = self.commit(items, ticket: ticket, template: template)
                Task { [weak self] in
                    let real = await work.value
                    guard let self else { return }
                    self.applyLate(real, replacing: keys, ticket: ticket, template: template)
                }
            }
        }
    }

    /// A later add never waits longer than this behind an earlier one.
    private static let maxWaitBehind: Double = 1.5

    /// Commits one add in order; its model keys, or nil when a clear won.
    private func commit(_ items: [ElementTrack.Resolved], ticket: ShapeTrack.AddOrder.Ticket,
                        template: HUDShape) -> [Int]? {
        guard addOrder.commit(ticket) else {
            ElementTrack.release(items.compactMap(\.track))
            return nil
        }
        return add(items, template: template)
    }

    private func add(_ items: [ElementTrack.Resolved], template: HUDShape) -> [Int] {
        var keys: [Int] = []
        var tracks: [Int: TrackAnchor] = [:]
        for item in items {
            var shape = template
            shape.box = [Double(item.rect.minX), Double(item.rect.minY), Double(item.rect.maxX), Double(item.rect.maxY)]
            let key = model.add(shape: shape)
            keys.append(key)
            if let track = item.track { tracks[key] = track }
        }
        let gone = Set(model.anchor(tracks))
        ElementTrack.release(tracks.filter { gone.contains($0.key) }.map(\.value),
                             keeping: tracks.filter { !gone.contains($0.key) }.map(\.value))
        return keys
    }

    /// A re-resolve that finished after its fallback was committed: move/upgrade the drawn box,
    /// draw what had no box, or remove a box whose target is positively gone. Nothing after a clear.
    private func applyLate(_ real: [ElementTrack.Resolved], replacing keys: [Int]?,
                           ticket: ShapeTrack.AddOrder.Ticket, template: HUDShape) {
        guard let keys, addOrder.survives(ticket) else {
            ElementTrack.release(real.compactMap(\.track))
            return
        }
        if keys.isEmpty {
            _ = add(real, template: template)
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
        let serial = listenSerial
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard let self, self.listenSerial == serial, self.model.orb == .idle, !self.audio.isBusy
            else { return }
            await self.handleWake(followUp: true)
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
