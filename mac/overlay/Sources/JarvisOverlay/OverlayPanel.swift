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
    /// when the fade ends. Below full opacity the window lets clicks through (a card dimmed
    /// over a highlight box must not eat clicks meant for it). Orders front only when it was
    /// out, so z-order stays arrival order instead of churning on every refit.
    func fade(to alpha: CGFloat) {
        guard alpha != targetAlpha else { return }
        targetAlpha = alpha
        fadeSerial += 1
        let serial = fadeSerial
        ignoresMouseEvents = alpha < 1
        if alpha > 0, !isVisible { orderFrontRegardless() }
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = Theme.reduceMotion ? 0 : 0.2
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            self.animator().alphaValue = alpha
        }, completionHandler: { [self] in
            MainActor.assumeIsolated {
                if serial == fadeSerial, alpha == 0 { orderOut(nil) }
            }
        })
    }

    /// Top-left global rect (HUD space) to this panel's AppKit frame; no-op when unchanged.
    func place(_ rect: CGRect) {
        let frame = CardPanel.flip(rect)
        if frame != self.frame { setFrame(frame, display: false) }
    }
}

/// One clickable panel per card, scattered over the visible screen by ScatterLayout (frames
/// stored in the model, so a card never moves when another comes or goes). The permission
/// card keeps its own panel top-right; parked cards share one chip-strip panel on the left
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
    private let chipWindow: HUDWindow
    private var cancellables: [AnyCancellable] = []

    init(surface: NSWindow) {
        self.surface = surface
        permissionWindow = HUDWindow()
        chipWindow = HUDWindow()
        super.init()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    func host(_ model: OverlayModel) {
        self.model = model
        permissionWindow.contentView = NSHostingView(rootView: PermissionTile(model: model, missing: []))
        chipWindow.contentView = NSHostingView(rootView: ChipStrip(model: model, chips: []))
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
                entry.host.rootView = CardTile(model: model, card: card)
                entry.card = card
                entries[key] = entry
                model.resize(key: key, size: entry.host.fittingSize, visible: visible)
            } else {
                let host = NSHostingView(rootView: CardTile(model: model, card: card))
                let window = HUDWindow()
                window.contentView = host
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
            model.setHovering(key, false)
        }

        let aside = model.layout.mode == .aside
        for card in model.cards {
            let key = card.listKey
            guard let entry = entries[key] else { continue }
            guard !aside, let frame = model.layout.frames[key] else {
                entry.window.fade(to: 0)
                model.setHovering(key, false)
                continue
            }
            entry.window.place(frame)
            // A highlight that appears under a card dims it; the card never moves.
            entry.window.fade(to: highlights.contains { $0.intersects(frame) } ? 0.25 : 1)
        }
        syncChips(model, visible: visible, highlights: highlights, aside: aside)
    }

    /// Shows the permission card top-right (16 pt inset) while a permission is missing and
    /// returns its frame as a keep-out. It never goes aside and is never cleared.
    private func syncPermission(_ model: OverlayModel, visible: CGRect) -> CGRect? {
        guard !model.missingPermissions.isEmpty,
              let host = permissionWindow.contentView as? NSHostingView<PermissionTile>
        else {
            permissionWindow.fade(to: 0)
            model.setHovering(PermissionTile.hoverID, false)
            return nil
        }
        host.rootView = PermissionTile(model: model, missing: model.missingPermissions)
        let size = host.fittingSize
        let frame = CGRect(x: visible.maxX - ScatterLayout.inset - size.width, y: visible.minY + ScatterLayout.inset,
                           width: size.width, height: size.height)
        permissionWindow.place(frame)
        permissionWindow.fade(to: 1)
        return frame
    }

    /// While aside, one strip panel holds every parked card as a chip on the edge away from the orb.
    private func syncChips(_ model: OverlayModel, visible: CGRect, highlights: [CGRect], aside: Bool) {
        let keys = model.layout.chipKeys
        guard aside, !keys.isEmpty, let host = chipWindow.contentView as? NSHostingView<ChipStrip> else {
            chipWindow.fade(to: 0)
            model.setHovering(ChipStrip.hoverID, false)
            return
        }
        let frames = ScatterLayout.chipFrames(keys: keys, visible: visible, orbOnRight: true)
        let strip = frames.values.reduce(CGRect.null) { $0.union($1) }
        let chips = model.cards.compactMap { card in
            frames[card.listKey].map { ChipStrip.Chip(card: card, offset: CGPoint(x: $0.minX - strip.minX, y: $0.minY - strip.minY)) }
        }
        host.rootView = ChipStrip(model: model, chips: chips)
        chipWindow.place(strip)
        chipWindow.fade(to: highlights.contains { $0.intersects(strip) } ? 0.25 : 1)
    }
}
