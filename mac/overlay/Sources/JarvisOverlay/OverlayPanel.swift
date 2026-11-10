import AppKit
import Combine
import QuartzCore
import SwiftUI
import OverlayCore

// Adapted from farzaa/clicky (MIT), see THIRD_PARTY_NOTICES.txt

private func configureHUDPanel(_ panel: NSPanel, ignoresMouseEvents: Bool) {
    panel.level = .screenSaver
    panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
    panel.ignoresMouseEvents = ignoresMouseEvents
    panel.isOpaque = false
    panel.backgroundColor = .clear
    panel.hasShadow = false
    panel.hidesOnDeactivate = false
    panel.isFloatingPanel = true
    panel.isReleasedWhenClosed = false
}

// shortcut: main display only, add per-screen panels if the owner uses 2 displays

/// Full-screen click-through surface for shapes and the orb.
final class OverlayPanel: NSPanel {
    init() {
        super.init(contentRect: NSScreen.main?.frame ?? .zero,
                   styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered,
                   defer: false)
        configureHUDPanel(self, ignoresMouseEvents: true)
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    func host(_ view: some View) {
        contentView = NSHostingView(rootView: view)
        orderFrontRegardless()
    }
}

/// Borderless clickable panel for one card, the permission card or the chip strip. Never
/// key or main, so a click on it never takes focus from the user's app.
final class HUDWindow: NSPanel {
    /// Bumped by every fade; a fade-out that ends after a later fade-in must not hide the window.
    private var fadeSerial = 0
    private var targetAlpha: CGFloat = 0
    /// Card panels only (nil on chips and the permission card, which stay put): called with the
    /// AppKit frame when a drag starts (`ended` false) and when it is dropped (true).
    var dragged: (@MainActor (CGRect, _ ended: Bool) -> Void)?
    /// AppKit rect a dragged panel stays fully inside (the visible frame of the overlay's screen).
    var dragBounds: CGRect = .zero
    /// The held mouse-down of a press on a card panel, its pointer and the panel origin then.
    private var press: (event: NSEvent, pointer: CGPoint, origin: CGPoint)?
    private(set) var isDragging = false

    init() {
        super.init(contentRect: .zero,
                   styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered,
                   defer: false)
        configureHUDPanel(self, ignoresMouseEvents: false)
        alphaValue = 0
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    /// Fades to `alpha` in 0.2 s ease-out (instant with Reduce Motion); 0 orders the window out
    /// when the fade ends. Clicks pass through until a fade to full opacity has finished: a
    /// card dimmed over a highlight box, or one still appearing, must not eat clicks meant for
    /// the app below. Orders front only when it was out, so z-order stays arrival order
    /// instead of churning on every refit.
    func fade(to alpha: CGFloat) {
        guard alpha != targetAlpha else { return }
        targetAlpha = alpha
        if alpha == 0 {
            press = nil
            isDragging = false
        }
        fadeSerial += 1
        let serial = fadeSerial
        ignoresMouseEvents = true
        if alpha > 0, !isVisible { orderFrontRegardless() }
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = Theme.reduceMotion ? 0 : 0.2
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            self.animator().alphaValue = alpha
        }, completionHandler: { [self] in
            MainActor.assumeIsolated {
                guard serial == fadeSerial else { return }
                if alpha == 0 { orderOut(nil) } else if alpha == 1 { ignoresMouseEvents = false }
            }
        })
    }

    /// Press-move-release on a card panel: past `ScatterLayout.dragThreshold` the panel follows
    /// the pointer and is dropped on release; the press never reaches the card, so no button
    /// fires. A shorter press is a click: the held mouse-down is delivered on release, with
    /// the mouse-up put back at the head of the queue so a control that tracks the mouse in
    /// its mouse-down still finds it. The panel is never key, so a drag never takes focus.
    override func sendEvent(_ event: NSEvent) {
        guard let dragged else { return super.sendEvent(event) }
        switch event.type {
        case .leftMouseDown:
            press = (event, NSEvent.mouseLocation, frame.origin)
        case .leftMouseDragged:
            guard let press else { return super.sendEvent(event) }
            let pointer = NSEvent.mouseLocation
            if !isDragging {
                guard ScatterLayout.isDrag(from: press.pointer, to: pointer) else { return }
                isDragging = true
                orderFrontRegardless()
                dragged(frame, false)
            }
            let moved = CGRect(x: press.origin.x + pointer.x - press.pointer.x,
                               y: press.origin.y + pointer.y - press.pointer.y,
                               width: frame.width, height: frame.height)
            setFrameOrigin(ScatterLayout.clamp(moved, into: dragBounds).origin)
        case .leftMouseUp:
            guard let held = press else { return super.sendEvent(event) }
            press = nil
            if isDragging {
                isDragging = false
                dragged(frame, true)
            } else {
                postEvent(event, atStart: true)
                super.sendEvent(held.event)
            }
        default:
            super.sendEvent(event)
        }
    }

    /// Top-left global rect (HUD space) to this panel's AppKit frame; no-op when unchanged.
    func place(_ rect: CGRect) {
        guard !isDragging else { return }
        let frame = CardPanel.flip(rect)
        if frame != self.frame { setFrame(frame, display: false) }
    }
}

/// One clickable panel per card, scattered over the visible screen by ScatterLayout (frames
/// stored in the model, so a card never moves when another comes or goes). The permission
/// card keeps its own panel top-right; parked cards become small chip panels on the left
/// edge. Everything outside these panels stays click-through.
@MainActor
final class CardPanel: NSObject {
    private struct Entry {
        let window: HUDWindow
        let host: NSHostingView<CardTile>
        var card: HUDCard
    }

    /// The orb/shape surface: all geometry follows the screen it is on.
    private let surface: NSWindow
    private var model: OverlayModel?
    private var entries: [String: Entry] = [:]
    private let permissionWindow: HUDWindow
    /// One small panel per parked chip, so the gaps between chips stay click-through.
    private var chipWindows: [String: (window: HUDWindow, host: NSHostingView<CardChipButton>)] = [:]
    /// Card keys as last stacked on screen, back to front.
    private var stacked: [String] = []
    private var cancellables: [AnyCancellable] = []

    init(surface: NSWindow) {
        self.surface = surface
        permissionWindow = HUDWindow()
        super.init()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    func host(_ model: OverlayModel) {
        self.model = model
        permissionWindow.contentView = NSHostingView(rootView: PermissionCard(missing: []))
        model.pointerOnPanel = { [weak self] in self?.pointerOnPanel() ?? false }
        // @Published fires before the value lands: sync on the next main-loop turn.
        let refit = { [weak self] (_: Any) in DispatchQueue.main.async { self?.sync() } }
        cancellables = [
            model.$cards.sink(receiveValue: refit),
            model.$missingPermissions.sink(receiveValue: refit),
            model.$layout.map(\.mode).removeDuplicates().sink(receiveValue: refit),
            model.$shapes.throttle(for: .milliseconds(100), scheduler: DispatchQueue.main, latest: true)
                .sink(receiveValue: refit),
        ]
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification,
                                               object: nil)
        sync()
    }

    /// AppKit frames are bottom-left-origin; HUD rects are top-left (primary display height).
    /// The flip is its own inverse.
    static func flip(_ rect: CGRect) -> CGRect {
        let height = NSScreen.screens.first?.frame.height ?? 0
        return CGRect(x: rect.minX, y: height - rect.maxY, width: rect.width, height: rect.height)
    }

    /// Screen and visible frame (no menu bar, no Dock) of the surface's screen, top-left.
    private var geometry: (screen: CGRect, visible: CGRect) {
        let screen = surface.screen ?? NSScreen.main
        return (Self.flip(screen?.frame ?? .zero), Self.flip(screen?.visibleFrame ?? .zero))
    }

    /// Whether the pointer rests on a shown, clickable card, chip or permission panel now.
    private func pointerOnPanel() -> Bool {
        let windows = entries.values.map(\.window) + chipWindows.values.map(\.window) + [permissionWindow]
        // A drag in progress holds the cards even where the clamp left the pointer outside.
        if windows.contains(where: \.isDragging) { return true }
        let frames = windows.filter { $0.isVisible && !$0.ignoresMouseEvents }.map(\.frame)
        return ScatterLayout.pointerRests(NSEvent.mouseLocation, onAnyOf: frames)
    }

    @objc private func screensChanged() {
        model?.reclamp(visible: geometry.visible)
        sync()
    }

    private func sync() {
        guard let model else { return }
        let (screen, visible) = geometry
        let highlights = model.shapes.map { ScatterLayout.shapeKeepOut($0.rect, screenWidth: screen.width) }
        var keepOut = [ScatterLayout.orbKeepOut(screen: screen)] + highlights
        if let permission = syncPermission(model, visible: visible) { keepOut.append(permission) }

        // Arrival order: a burst is placed oldest first, each card once it is measured.
        for card in model.cards {
            let key = card.listKey
            if var entry = entries[key] {
                guard entry.card != card else { continue }
                entry.host.rootView = CardTile(card: card)
                entry.card = card
                entries[key] = entry
                model.resize(key: key, size: entry.host.fittingSize, visible: visible, keepOut: keepOut)
            } else {
                let host = NSHostingView(rootView: CardTile(card: card))
                let window = HUDWindow()
                window.contentView = host
                window.dragged = { [weak self] frame, ended in self?.cardDragged(key, frame: frame, ended: ended) }
                entries[key] = Entry(window: window, host: host, card: card)
            }
        }
        for card in model.cards where model.layout.frames[card.listKey] == nil {
            guard let entry = entries[card.listKey] else { continue }
            model.place(key: card.listKey, size: entry.host.fittingSize, visible: visible, keepOut: keepOut)
        }

        // Retired, cleared or idle-cleared cards fade out and go.
        let live = Set(model.cards.map(\.listKey))
        for (key, entry) in entries where !live.contains(key) {
            entry.window.fade(to: 0)
            entries[key] = nil
        }

        let aside = model.layout.mode == .aside
        let dragBounds = Self.flip(visible)
        for card in model.cards {
            let key = card.listKey
            guard let entry = entries[key], !entry.window.isDragging else { continue }
            entry.window.dragBounds = dragBounds
            guard !aside, let frame = model.layout.frames[key] else {
                entry.window.fade(to: 0)
                continue
            }
            entry.window.place(frame)
            // A highlight that appears under a card dims it; the card never moves.
            entry.window.fade(to: highlights.contains { $0.intersects(frame) } ? 0.25 : 1)
        }
        restack(aside ? [] : model.layout.order.filter { entries[$0] != nil })
        syncChips(model, visible: visible, highlights: highlights, aside: aside)
    }

    /// Stacks the shown card panels as `layout.order` (back to front), the one z-order for
    /// arrival, re-place, drag and aside -> restore. Only what changed is ordered: new cards on
    /// top go front one by one; any other change restacks all (a handful of calls, no churn).
    private func restack(_ stack: [String]) {
        let kept = stacked.filter { stack.contains($0) }
        for key in stack[(stack.starts(with: kept) ? kept.count : 0)...] {
            entries[key]?.window.orderFrontRegardless()
        }
        stacked = stack
    }

    /// A drag started (the card comes to the front) or ended (it stays where it was dropped).
    private func cardDragged(_ key: String, frame: CGRect, ended: Bool) {
        guard let model else { return }
        model.drag(key: key, to: Self.flip(frame), visible: geometry.visible, ended: ended)
        if ended { sync() }
    }

    /// Shows the permission card top-right (16 pt inset) while a permission is missing and
    /// returns its frame as a keep-out. It never goes aside and is never cleared.
    private func syncPermission(_ model: OverlayModel, visible: CGRect) -> CGRect? {
        guard !model.missingPermissions.isEmpty,
              let host = permissionWindow.contentView as? NSHostingView<PermissionCard>
        else {
            permissionWindow.fade(to: 0)
            return nil
        }
        host.rootView = PermissionCard(missing: model.missingPermissions)
        let size = host.fittingSize
        let frame = CGRect(x: visible.maxX - ScatterLayout.inset - size.width, y: visible.minY + ScatterLayout.inset,
                           width: size.width, height: size.height)
        permissionWindow.place(frame)
        permissionWindow.fade(to: 1)
        return frame
    }

    /// While aside, every parked card is a chip in its own small panel on the edge away from the orb.
    private func syncChips(_ model: OverlayModel, visible: CGRect, highlights: [CGRect], aside: Bool) {
        let frames = aside ? ScatterLayout.chipFrames(keys: model.layout.chipKeys, visible: visible, orbOnRight: true) : [:]
        for (key, chip) in chipWindows where frames[key] == nil {
            chip.window.fade(to: 0)
            chipWindows[key] = nil
        }
        for card in model.cards {
            guard let frame = frames[card.listKey] else { continue }
            let button = CardChipButton(model: model, card: card)
            let chip: (window: HUDWindow, host: NSHostingView<CardChipButton>)
            if let existing = chipWindows[card.listKey] {
                chip = existing
                chip.host.rootView = button
            } else {
                chip = (HUDWindow(), NSHostingView(rootView: button))
                chip.window.contentView = chip.host
                chipWindows[card.listKey] = chip
            }
            chip.window.place(frame)
            chip.window.fade(to: highlights.contains { $0.intersects(frame) } ? 0.25 : 1)
        }
    }
}
