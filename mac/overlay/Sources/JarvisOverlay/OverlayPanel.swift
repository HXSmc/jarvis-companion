import AppKit
import Combine
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

/// Clickable card column, top-right (16 pt inset), sized to the SwiftUI content.
final class CardPanel: NSPanel {
    private let screenFrame: CGRect
    private var cardsCancellable: AnyCancellable?

    init() {
        screenFrame = NSScreen.main?.visibleFrame ?? .zero
        super.init(contentRect: NSRect(x: screenFrame.maxX - 16 - 360,
                                       y: screenFrame.maxY - 16,
                                       width: 360,
                                       height: 0),
                   styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered,
                   defer: false)
        configureHUDPanel(self, ignoresMouseEvents: false)
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    func host(_ model: OverlayModel) {
        contentView = NSHostingView(rootView: CardColumnView(model: model))
        // @Published fires before the value lands: measure on the next main-loop turn.
        cardsCancellable = model.$cards.sink { [weak self] cards in
            DispatchQueue.main.async { self?.fit(cards: cards) }
        }
        fit(cards: model.cards)
    }

    /// Resize to the content's fittingSize whenever cards change; hidden when there are no cards.
    private func fit(cards: [HUDCard]) {
        if cards.isEmpty {
            orderOut(nil)
            return
        }
        guard let content = contentView else { return }
        let size = content.fittingSize
        setFrame(NSRect(x: screenFrame.maxX - 16 - size.width,
                        y: screenFrame.maxY - 16 - size.height,
                        width: size.width,
                        height: size.height),
                 display: false)
        orderFrontRegardless()
    }
}
