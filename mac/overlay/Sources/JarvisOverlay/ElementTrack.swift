import AppKit
import ApplicationServices
import OverlayCore

/// App side of ShapeTrack: add-time re-resolve (AX element, shared OCR refind, window match), AX
/// element and scroll-bar reads, the scroll-wheel monitor, the content check and the gutter
/// re-find for line bands. AX elements live in the lock-protected `Registry`; only Int handles
/// cross threads, and an element is used only inside the closure that created it or copied it
/// out under the lock. Reads run on a concurrent queue (one batch per app, so a hung app blocks
/// only its own). Secure text field values are never read. Nothing read here leaves the process
/// or is logged. Only installing/removing the scroll monitor runs on the main actor.
/// Sendable by confinement: `tracker` is touched only on `trackerQueue`, `monitor` only on main.
final class ElementTrack: ElementSource, @unchecked Sendable {
    /// What a re-resolve found: the fresh rect and, for AX, the element. `verified` false: the
    /// element was only looked up by position at the stale spot, nothing re-found its label.
    struct Found {
        var rect: CGRect
        var element: AXUIElement?
        var verified = true
    }

    /// A new shape's box and anchor (nil track = static, as before).
    struct Resolved: Sendable {
        var rect: CGRect
        var track: TrackAnchor?
    }

    weak var tracker: ShapeFollowTracker?
    private let trackerQueue: DispatchQueue
    /// Concurrent: a hung app blocks only its own batch (the tracker keeps one in flight per pid).
    private let readQueue = DispatchQueue(label: "jarvis.overlay.element-read", qos: .utility, attributes: .concurrent)
    @MainActor private static var monitor: Any?

    init(trackerQueue: DispatchQueue) { self.trackerQueue = trackerQueue }

    // MARK: registry

    private enum Entry {
        case element(AXUIElement, clip: AXUIElement?, secure: Bool)
        case scrollBar(AXUIElement)
    }

    private final class Registry: @unchecked Sendable {
        private let lock = NSLock()
        private var entries: [Int: Entry] = [:]
        private var next = 0

        func add(_ entry: Entry) -> Int {
            lock.lock()
            defer { lock.unlock() }
            next += 1
            entries[next] = entry
            return next
        }

        func lookup(_ handles: [Int]) -> [Int: Entry] {
            lock.lock()
            defer { lock.unlock() }
            return handles.reduce(into: [:]) { out, handle in out[handle] = entries[handle] }
        }

        func remove(_ handles: [Int]) {
            lock.lock()
            defer { lock.unlock() }
            for handle in handles { entries[handle] = nil }
        }
    }

    private static let registry = Registry()

    /// Gives back the AX refs of anchors that were never handed to the tracker, except handles
    /// that `kept` anchors still use (a scroll bar shared by sibling bands).
    static func release(_ tracks: [TrackAnchor], keeping kept: [TrackAnchor] = []) {
        let keep = Set(kept.flatMap(handles))
        registry.remove(tracks.flatMap(handles).filter { !keep.contains($0) })
    }

    private static func handles(_ track: TrackAnchor) -> [Int] {
        var out = track.window.scrollBar.map { [$0] } ?? []
        if case .element(let anchor) = track { out.append(anchor.handle) }
        return out
    }

    // MARK: ElementSource (called on the tracker queue; returns at once)

    func read(pid: Int32, handles: [Int: UInt32]) {
        readQueue.async { [self] in
            var out: [Int: ElementRead] = [:]
            for (handle, entry) in Self.registry.lookup(Array(handles.keys)) { out[handle] = Self.read(entry) }
            // Stamp element reads after the reads: placeElement adds only window motion since this
            // frame. A scroll-bar value needs no frame, so a bar-only batch makes no window-list call.
            let framed = Array(out.filter { $0.value.scrollValue == nil }.keys)
            if !framed.isEmpty {
                let frames = CGWindowSource().sample(ids: Array(Set(framed.compactMap { handles[$0] })))
                for handle in framed {
                    if let frame = handles[handle].flatMap({ frames[$0]?.frame }) {
                        out[handle]?.windowFrame = frame
                    } else {
                        out[handle] = ElementRead(status: .timeout)
                    }
                }
            }
            let reads = out
            trackerQueue.async { [self] in tracker?.receive(pid: pid, reads: reads) }
        }
    }

    func refindBands(window: UInt32, lines: [Int: ClosedRange<Int>]) {
        Task.detached(priority: .utility) { [self] in
            let found = await Self.findBands(window: window, lines: lines)
            trackerQueue.async { [self] in
                tracker?.receiveBands(window: window, rects: found.rects, windowFrame: found.frame)
            }
        }
    }

    func checkContent(_ rects: [Int: CGRect]) {
        Task.detached(priority: .utility) { [self] in
            let grids = await Self.grids(rects)
            trackerQueue.async { [self] in tracker?.receiveContent(grids, rects: rects) }
        }
    }

    func setScrollMonitor(_ on: Bool) {
        DispatchQueue.main.async { [self] in
            MainActor.assumeIsolated {
                if on { installMonitor() } else { Self.removeMonitor() }
            }
        }
    }

    func release(_ handles: Set<Int>) { Self.registry.remove(Array(handles)) }

    @MainActor private func installMonitor() {
        guard Self.monitor == nil else { return }
        // Scroll-wheel events only: no key events, no event tap (no Input Monitoring prompt).
        // Only the pointer location is used; the event's deltas and content are not read.
        Self.monitor = NSEvent.addGlobalMonitorForEvents(matching: .scrollWheel) { [weak self] _ in
            let point = MainActor.assumeIsolated { () -> CGPoint in
                let location = NSEvent.mouseLocation
                return CGPoint(x: location.x, y: (NSScreen.screens.first?.frame.height ?? 0) - location.y)
            }
            guard let self else { return }
            self.trackerQueue.async { [self] in self.tracker?.scrollWheel(at: point) }
        }
    }

    @MainActor private static func removeMonitor() {
        guard let monitor else { return }
        NSEvent.removeMonitor(monitor)
        self.monitor = nil
    }

    // MARK: add-time re-resolve (call off the main actor)

    /// A whole-window shape re-found in the CG list (no OCR, no AX); nil = not a window shape.
    static func windowShape(stale: CGRect) -> [Resolved]? {
        let windows = WindowFollower.normalWindows()
        guard let rect = ShapeTrack.matchWindow(stale: stale, windows: windows) else { return nil }
        return place(fresh: rect, stale: stale, windows: windows, scrollBar: false, windowPick: true)
    }

    /// The shape label matched near its stale rect in a fresh OCR pass shared by every shape
    /// of the same event batch (one capture + OCR per second at most); a grounded shape only in
    /// the window of its original rect.
    static func refind(label: String, stale: CGRect, grounded: Bool) async -> Found? {
        guard !label.isEmpty else { return nil }
        let observations = await ocrShare.task().value
        return ShapeTrack.refind(label: label, stale: stale, observations: observations,
                                 windows: WindowFollower.normalWindows(), grounded: grounded).map { Found(rect: $0) }
    }

    /// The capture's OCR lines and windows in points: the items a grounded shape's rect came from.
    static func captured(_ screen: ScreenPayload?) -> [CGRect] {
        guard let screen, screen.scale > 0 else { return [] }
        func rect(_ box: [Double], _ scale: Double) -> CGRect? {
            box.count == 4 ? CGRect(x: box[0] / scale, y: box[1] / scale,
                                    width: (box[2] - box[0]) / scale, height: (box[3] - box[1]) / scale) : nil
        }
        return screen.ocr.compactMap { rect($0.box, screen.scale) } + screen.windows.compactMap { rect($0.box, 1) }
    }

    typealias Observations = [(text: String, rect: CGRect)]

    /// The running (or < 1 s old) capture + OCR, else a new one. The screen text is dropped 1 s
    /// after it was read, so no OCR result stays in memory.
    private final class OCRShare: @unchecked Sendable {
        private let lock = NSLock()
        private var started: TimeInterval = -.infinity
        private var running: Task<Observations, Never>?
        private var serial = 0

        func task() -> Task<Observations, Never> {
            lock.lock()
            defer { lock.unlock() }
            let time = ProcessInfo.processInfo.systemUptime
            if let running, time - started <= 1 { return running }
            let task = Task.detached(priority: .userInitiated) { await ElementTrack.screenText() }
            started = time
            running = task
            serial += 1
            let mine = serial
            Task.detached { [self] in
                _ = await task.value
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                drop(mine)
            }
            return task
        }

        private func drop(_ mine: Int) {
            lock.lock()
            defer { lock.unlock() }
            if serial == mine { running = nil }
        }
    }

    private static let ocrShare = OCRShare()

    /// One main-display capture + OCR, boxes in global points.
    private static func screenText() async -> Observations {
        guard CGPreflightScreenCaptureAccess(), let shot = try? await ScreenContext.captureMainDisplay() else { return [] }
        let scale = shot.scale
        return ScreenContext.ocrText(in: shot.image).map { pair in
            (text: pair.text, rect: CGRect(x: pair.box[0] / scale, y: pair.box[1] / scale,
                                           width: (pair.box[2] - pair.box[0]) / scale,
                                           height: (pair.box[3] - pair.box[1]) / scale))
        }
    }

    /// Position lookup: the element under the rect's centre, asked of the app owning the window
    /// there (app-scoped timeout; the global AX timeout is never changed).
    static func lookup(near rect: CGRect) -> Found? {
        guard let window = ShapeFollow.anchor(rect: rect, windows: WindowFollower.normalWindows()) else { return nil }
        let app = AXUIElementCreateApplication(window.pid)
        AXUIElementSetMessagingTimeout(app, 0.1)
        var element: AXUIElement?
        guard AXUIElementCopyElementAtPosition(app, Float(rect.midX), Float(rect.midY), &element) == .success,
              let element
        else { return nil }
        return Found(rect: rect, element: element, verified: false)
    }

    /// The re-resolved shape with its best anchor: element (tier A) when an AX element carries it,
    /// else the window (tier C) at the fresh rect, else at the original rect. [] = positively gone,
    /// or a rect that was never grounded and whose label was not re-found.
    /// A grounded shape's target must lie in the window that holds its original rect: a target
    /// re-found in another window (the same label shown twice) is rejected for the fallback.
    static func anchor(stale: CGRect, label: String, found: Found?, grounded: Bool) -> [Resolved] {
        let windows = WindowFollower.normalWindows()
        let kept = found.flatMap { !grounded || ShapeTrack.sameWindow($0.rect, as: stale, windows: windows) ? $0 : nil }
        if let kept, let element = kept.element ?? lookup(near: kept.rect)?.element,
           let resolved = elementAnchor(element, box: kept.rect, label: label, windows: windows,
                                        home: grounded ? stale : nil, mustName: !kept.verified) {
            return resolved
        }
        return place(fresh: kept?.rect, stale: stale, windows: windows, scrollBar: true, grounded: grounded)
    }

    /// Budget fallback: the original rect, window-anchored (tier C); nothing for a rect that was
    /// never grounded. No AX here, so a hung app cannot slow it; the content check covers its
    /// scroll signal.
    static func fallback(stale: CGRect, grounded: Bool) -> [Resolved] {
        let windows = WindowFollower.normalWindows()
        return place(fresh: nil, stale: stale, windows: windows, scrollBar: false, grounded: grounded,
                     windowPick: grounded && ShapeTrack.matchWindow(stale: stale, windows: windows) != nil)
    }

    /// Line bands for a `.lines` event, each anchored to its window with the scroll signal.
    /// The service scrolls the editor to the change while this runs, so a pass that finds the
    /// wanted lines off view (or no gutter yet) is tried again: at 0.9 s, 1.9 s and 2.9 s plus OCR
    /// time. `report` gets one reason code for the debug page (codes and line numbers only).
    static func resolveBands(_ ranges: [ClosedRange<Int>], file: String,
                             report: @escaping @Sendable (String) -> Void) async -> [Resolved] {
        var pass = LineBandFit.Pass(reason: "no_ranges")
        var detail = ""
        for attempt in 1...bandTries {
            try? await Task.sleep(nanoseconds: attempt == 1 ? 900_000_000 : 1_000_000_000)
            (pass, detail) = await ScreenContext.lineBands(ranges: ranges, file: file, partial: attempt == bandTries)
            detail += " try=\(attempt)"
            if !pass.bands.isEmpty || pass.reason == "no_permission" || pass.reason == "no_ranges" { break }
        }
        let wanted = ranges.map { "\($0.lowerBound)-\($0.upperBound)" }.prefix(3).joined(separator: ",")
        guard !pass.bands.isEmpty else {
            report(pass.reason + detail + " want=\(wanted)")
            return []
        }
        let windows = WindowFollower.normalWindows()
        var anchors = pass.bands.map { ShapeFollow.anchor(rect: $0.rect, windows: windows) }
        // One scroll bar per band window (all bands come from the same window), shared.
        let bar = anchors.compactMap { $0 }.first.flatMap {
            scrollBar(at: CGPoint(x: $0.rect.midX, y: $0.rect.midY), pid: $0.pid)
        }
        for index in anchors.indices { anchors[index]?.scrollBar = bar }
        report("ok bands=\(pass.bands.count) anchored=\(anchors.compactMap { $0 }.count)" + detail + " want=\(wanted)")
        return zip(pass.bands, anchors).map { band, anchor in
            Resolved(rect: band.rect, track: anchor.map { .band(BandAnchor(window: $0, lines: band.lines)) })
        }
    }

    private static let bandTries = 3

    /// Tier C (or static) placement; `scrollBar`: look up the scroll bar under the box so a
    /// keyboard or scroll-bar scroll hides it too (not for whole-window boxes).
    private static func place(fresh: CGRect?, stale: CGRect, windows: [WindowFollower.Window],
                              scrollBar lookUp: Bool, grounded: Bool = true, windowPick: Bool = false) -> [Resolved] {
        switch ShapeTrack.placement(fresh: fresh, stale: stale, windows: windows, screens: [mainScreen],
                                    grounded: grounded, windowPick: windowPick) {
        case .anchored(var anchor):
            if lookUp, !anchor.rect.contains(anchor.windowFrame) {
                anchor.scrollBar = scrollBar(at: CGPoint(x: anchor.rect.midX, y: anchor.rect.midY), pid: anchor.pid)
            }
            return [Resolved(rect: anchor.rect, track: .window(anchor))]
        case .unanchored(let rect): return [Resolved(rect: rect, track: nil)]
        case .gone: return []
        }
    }

    /// Main display in global top-left points without AppKit (CoreGraphics is thread-safe).
    private static var mainScreen: CGRect { CGDisplayBounds(CGMainDisplayID()) }

    /// No kAXValue here: it is read separately, and never for a secure text field.
    private static let infoAttributes: [String] = [
        kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
        kAXPositionAttribute, kAXSizeAttribute, kAXWindowAttribute, kAXParentAttribute,
    ]

    /// One AX element as read at add time.
    private struct Inspected {
        var element: AXUIElement
        var pid: pid_t
        var role: String
        var frame: CGRect
        var secure: Bool
        var title: String
        var description: String
        var value: String
        var window: AXUIElement?
        var parent: AXUIElement?
        var label: String { title.isEmpty ? description : title }
    }

    /// One IPC for role/subrole/title/description/frame/window/parent, one more for the value
    /// unless the element may be a secure text field.
    private static func inspect(_ element: AXUIElement) -> Inspected? {
        AXUIElementSetMessagingTimeout(element, 0.1)
        var pid: pid_t = 0
        let (status, info) = copy(element, infoAttributes)
        guard status == .success, AXUIElementGetPid(element, &pid) == .success,
              let rect = frame(info[4], info[5])
        else { return nil }
        // A subrole that failed to read (not merely absent) may hide a secure field: never read its value.
        let unread = axError(info[1]).map { $0 != .noValue && $0 != .attributeUnsupported } ?? false
        let secure = (info[1] as? String) == kAXSecureTextFieldSubrole || unread
        return Inspected(element: element, pid: pid, role: info[0] as? String ?? "", frame: rect, secure: secure,
                         title: info[2] as? String ?? "", description: info[3] as? String ?? "",
                         value: secure ? "" : stringValue(element),
                         window: axElement(info[6]), parent: axElement(info[7]))
    }

    /// Tier A anchor, registering the element. nil = the element cannot carry the box (fall back);
    /// [] = it can but the box is mostly off-screen (positively gone). `home`: the original rect
    /// of a grounded shape; the element's own window must be the one holding it.
    private static func elementAnchor(_ element: AXUIElement, box: CGRect, label: String,
                                      windows: [WindowFollower.Window], home: CGRect?, mustName: Bool) -> [Resolved]? {
        guard var target = inspect(element) else { return nil }
        var accepted = ShapeTrack.acceptElement(box: box, elementFrame: target.frame, role: target.role, value: target.value,
                                                label: target.label, shapeLabel: label, mustName: mustName)
        // Safari draws the URL as a child of its address field: the box takes the field's frame.
        if accepted?.fit != .snap, !ShapeTrack.textRoles.contains(target.role),
           let field = ancestor(from: target.parent, roles: ShapeTrack.textRoles, levels: 3).flatMap(inspect) {
            if field.secure {
                // The child's text was read before its secure field was found: drop it, never poll it.
                target.value = ""
                target.secure = true
                accepted = ShapeTrack.acceptElement(box: box, elementFrame: target.frame, role: target.role, value: "",
                                                    label: target.label, shapeLabel: label, mustName: mustName)
            } else if let snap = ShapeTrack.acceptElement(box: box, elementFrame: field.frame, role: field.role,
                                                          value: field.value, label: field.label, shapeLabel: label),
                      snap.fit == .snap {
                target = field
                accepted = snap
            }
        }
        guard var fit = accepted else { return nil }
        // The field's visible frame may be a wrapper around it (Safari's focus ring): snap to that.
        if fit.fit == .snap {
            let chain = frames(from: target.parent, levels: 3)
            if let index = ShapeTrack.fieldWrapper(box: box, field: target.frame, ancestors: chain.map { $0.frame }),
               var wrapper = inspect(chain[index].element) {
                if target.secure {
                    wrapper.value = ""
                    wrapper.secure = true
                }
                target = wrapper
                fit.rect = wrapper.frame.insetBy(dx: -ShapeTrack.pad, dy: -ShapeTrack.pad)
            }
        }
        let pid = target.pid
        // The element's own window, not the one on top of it; hit-test only as the fallback.
        let ownFrame = target.window.flatMap { window -> CGRect? in
            AXUIElementSetMessagingTimeout(window, 0.1)
            let (status, values) = copy(window, frameAttributes)
            return status == .success ? frame(values[0], values[1]) : nil
        }
        let hit = ShapeFollow.anchor(rect: fit.rect, windows: windows).flatMap { anchor -> WindowFollower.Window? in
            anchor.pid == pid ? (anchor.windowID, anchor.pid, anchor.windowFrame) : nil
        }
        guard let window = ownFrame.flatMap({ ShapeTrack.windowFor(elementWindowFrame: $0, pid: pid, windows: windows) }) ?? hit,
              home.map({ window.id == ShapeTrack.owner(of: $0, windows: windows) }) ?? true
        else { return nil }
        // A box covering most of its window is not the asked thing: fall back to the re-found rect.
        guard !ShapeTrack.windowSized(fit.rect, window: window.frame) else { return nil }
        guard ShapeTrack.onScreen(fit.rect, screens: [mainScreen]) else { return [] }
        let clip = ancestor(from: target.parent, roles: [kAXScrollAreaRole, "AXWebArea"])
        AXUIElementSetMessagingTimeout(target.element, 0.05)
        if let clip { AXUIElementSetMessagingTimeout(clip, 0.05) }
        let handle = registry.add(.element(target.element, clip: clip, secure: target.secure))
        let anchor = ElementAnchor(fit: fit.fit,
                                   window: ShapeAnchor(windowID: window.id, pid: pid, windowFrame: window.frame, rect: fit.rect),
                                   handle: handle, elementFrame: target.frame,
                                   text: ShapeTrack.elementText(title: target.title, description: target.description,
                                                                value: target.value))
        return [Resolved(rect: fit.rect, track: .element(anchor))]
    }

    /// The vertical scroll bar of the scroll area under `point` in `pid`'s app, registered; nil
    /// when there is none or it has no numeric value (wheel signal only, e.g. Electron editors).
    private static func scrollBar(at point: CGPoint, pid: Int32) -> Int? {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.1)
        var element: AXUIElement?
        guard AXUIElementCopyElementAtPosition(app, Float(point.x), Float(point.y), &element) == .success,
              let element
        else { return nil }
        AXUIElementSetMessagingTimeout(element, 0.1)
        let (status, info) = copy(element, [kAXRoleAttribute, kAXParentAttribute])
        guard status == .success else { return nil }
        let area = (info[0] as? String) == kAXScrollAreaRole
            ? element : ancestor(from: axElement(info[1]), roles: [kAXScrollAreaRole])
        guard let area else { return nil }
        let (barStatus, values) = copy(area, [kAXVerticalScrollBarAttribute])
        guard barStatus == .success, let bar = axElement(values[0]) else { return nil }
        AXUIElementSetMessagingTimeout(bar, 0.05)
        guard read(.scrollBar(bar)).scrollValue != nil else { return nil }
        return registry.add(.scrollBar(bar))
    }

    /// `start` and up to `levels - 1` of its parents with their frames (one IPC per level).
    private static func frames(from start: AXUIElement?, levels: Int) -> [(element: AXUIElement, frame: CGRect)] {
        var out: [(element: AXUIElement, frame: CGRect)] = []
        var node = start
        for _ in 0..<levels {
            guard let current = node else { break }
            AXUIElementSetMessagingTimeout(current, 0.1)
            let (status, values) = copy(current, [kAXPositionAttribute, kAXSizeAttribute, kAXParentAttribute])
            guard status == .success, let rect = frame(values[0], values[1]) else { break }
            out.append((element: current, frame: rect))
            node = axElement(values[2])
        }
        return out
    }

    /// First node with one of `roles`, starting at `start` and walking up at most `levels`
    /// parents (one IPC per level).
    private static func ancestor(from start: AXUIElement?, roles: Set<String>, levels: Int = 8) -> AXUIElement? {
        var node = start
        for _ in 0..<levels {
            guard let current = node else { return nil }
            AXUIElementSetMessagingTimeout(current, 0.1)
            let (status, values) = copy(current, [kAXRoleAttribute, kAXParentAttribute])
            guard status == .success else { return nil }
            if roles.contains(values[0] as? String ?? "") { return current }
            node = axElement(values[1])
        }
        return nil
    }

    // MARK: reads (read queue)

    /// kAXValue last, so a secure field reads the first four only.
    private static let elementAttributes: [String] = [
        kAXPositionAttribute, kAXSizeAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute,
    ]
    private static let frameAttributes: [String] = [kAXPositionAttribute, kAXSizeAttribute]

    /// One IPC on the element (+ one on its clip); never more.
    private static func read(_ entry: Entry) -> ElementRead {
        switch entry {
        case .scrollBar(let bar):
            var value: AnyObject?
            guard AXUIElementCopyAttributeValue(bar, kAXValueAttribute as CFString, &value) == .success,
                  let number = value as? NSNumber
            else { return ElementRead(status: .timeout) }
            return ElementRead(status: .ok, scrollValue: number.doubleValue)
        case .element(let element, let clip, let secure):
            let (status, values) = copy(element, secure ? Array(elementAttributes.prefix(4)) : elementAttributes)
            if status == .invalidUIElement || values.first.map(isInvalid) == true { return ElementRead(status: .invalid) }
            guard status == .success, let elementFrame = frame(values[0], values[1]) else {
                return ElementRead(status: .timeout)
            }
            let text = ShapeTrack.elementText(title: values[2] as? String ?? "",
                                              description: values[3] as? String ?? "",
                                              value: secure ? "" : values[4] as? String ?? "")
            var clipFrame: CGRect?
            if let clip {
                let (clipStatus, clipValues) = copy(clip, frameAttributes)
                if clipStatus == .success { clipFrame = frame(clipValues[0], clipValues[1]) }
            }
            return ElementRead(status: .ok, frame: elementFrame, text: text, clip: clipFrame)
        }
    }

    /// One IPC. With option 0 a failing attribute comes back as an AXValue error in the array.
    private static func copy(_ element: AXUIElement, _ attributes: [String]) -> (AXError, [AnyObject]) {
        var values: CFArray?
        let status = AXUIElementCopyMultipleAttributeValues(
            element, attributes as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &values
        )
        guard status == .success, let raw = values as [AnyObject]?, raw.count == attributes.count
        else { return (status == .success ? .failure : status, []) }
        return (.success, raw)
    }

    private static func stringValue(_ element: AXUIElement) -> String {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value) == .success else { return "" }
        return value as? String ?? ""
    }

    private static func frame(_ position: AnyObject, _ size: AnyObject) -> CGRect? {
        var point = CGPoint.zero, dimensions = CGSize.zero
        guard let position = axValue(position, .cgPoint), let size = axValue(size, .cgSize),
              AXValueGetValue(position, .cgPoint, &point), AXValueGetValue(size, .cgSize, &dimensions),
              dimensions.width > 0, dimensions.height > 0
        else { return nil }
        return CGRect(origin: point, size: dimensions)
    }

    private static func isInvalid(_ object: AnyObject) -> Bool { axError(object) == .invalidUIElement }

    /// The AXError a multi-attribute read put in place of a value; nil = a real value.
    private static func axError(_ object: AnyObject) -> AXError? {
        var error = AXError.success
        guard let value = axValue(object, .axError), AXValueGetValue(value, .axError, &error) else { return nil }
        return error
    }

    private static func axValue(_ object: AnyObject, _ type: AXValueType) -> AXValue? {
        guard CFGetTypeID(object) == AXValueGetTypeID() else { return nil }
        let value = object as! AXValue  // type id checked above; `as?` on a CF type always succeeds
        return AXValueGetType(value) == type ? value : nil
    }

    private static func axElement(_ object: AnyObject?) -> AXUIElement? {
        guard let object, CFGetTypeID(object) == AXUIElementGetTypeID() else { return nil }
        return (object as! AXUIElement)  // type id checked above
    }

    // MARK: content check (detached utility task)

    /// One capture of just the boxes' area; each rect reduced to its 16x16 luma grid.
    private static func grids(_ rects: [Int: CGRect]) async -> [Int: [UInt8]] {
        let region = rects.values.reduce(CGRect.null) { $0.union($1) }.intersection(mainScreen).integral
        guard !region.isNull, !region.isEmpty, CGPreflightScreenCaptureAccess(),
              let shot = try? await ScreenContext.captureMainDisplay(region: region)
        else { return [:] }
        let scale = CGFloat(shot.scale)
        let bounds = CGRect(x: 0, y: 0, width: shot.image.width, height: shot.image.height)
        return rects.compactMapValues { rect in
            let crop = CGRect(x: (rect.minX - region.minX) * scale, y: (rect.minY - region.minY) * scale,
                              width: rect.width * scale, height: rect.height * scale).integral.intersection(bounds)
            guard !crop.isNull, let image = shot.image.cropping(to: crop) else { return nil }
            return lumaGrid(of: image)
        }
    }

    /// The image scaled into a 64x64 gray bitmap, then ShapeTrack.lumaGrid.
    private static func lumaGrid(of image: CGImage) -> [UInt8]? {
        let side = 64
        var pixels = [UInt8](repeating: 0, count: side * side)
        let drawn = pixels.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(data: buffer.baseAddress, width: side, height: side, bitsPerComponent: 8,
                                          bytesPerRow: side, space: CGColorSpaceCreateDeviceGray(),
                                          bitmapInfo: CGImageAlphaInfo.none.rawValue)
            else { return false }
            context.interpolationQuality = .medium
            context.draw(image, in: CGRect(x: 0, y: 0, width: side, height: side))
            return true
        }
        return drawn ? ShapeTrack.lumaGrid(luma: pixels, width: side, height: side) : nil
    }

    // MARK: band re-find (detached utility task)

    /// Main-display capture cropped to the window, OCR, fresh gutter fit: one re-find.
    private static func findBands(window: UInt32, lines: [Int: ClosedRange<Int>]) async -> (rects: [Int: CGRect], frame: CGRect) {
        guard let frame = CGWindowSource().sample(ids: [window])[window]?.frame,
              CGPreflightScreenCaptureAccess(),
              let shot = try? await ScreenContext.captureMainDisplay()
        else { return ([:], .zero) }
        let scale = CGFloat(shot.scale)
        let crop = CGRect(x: frame.minX * scale, y: frame.minY * scale, width: frame.width * scale, height: frame.height * scale)
            .integral.intersection(CGRect(x: 0, y: 0, width: shot.image.width, height: shot.image.height))
        guard !crop.isNull, let image = shot.image.cropping(to: crop) else { return ([:], frame) }
        var digits: [(line: Int, centre: CGPoint, right: CGFloat)] = []
        for pair in ScreenContext.ocrText(in: image) {
            guard let line = ShapeTrack.lineNumber(pair.text) else { continue }
            let centre = CGPoint(x: (crop.minX + (pair.box[0] + pair.box[2]) / 2) / scale,
                                 y: (crop.minY + (pair.box[1] + pair.box[3]) / 2) / scale)
            digits.append((line, centre, (crop.minX + pair.box[2]) / scale))
        }
        guard let gutter = ShapeTrack.gutterMarks(digits: digits, window: frame) else { return ([:], frame) }
        let x = gutter.right + 4
        let width = frame.maxX - 8 - x
        guard width > 0 else { return ([:], frame) }
        let rects = lines.compactMapValues {
            ShapeTrack.placeBand(lines: $0, marks: gutter.marks, x: x, width: width, clip: frame)
        }
        return (rects, frame)
    }
}
