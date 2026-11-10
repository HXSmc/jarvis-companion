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

/// Clickable card column, top-right (16 pt inset) unless a shape box sits under
/// it (then top-left), sized to the SwiftUI content and clamped above the orb zone.
final class CardPanel: NSPanel {
    private let screenFrame: CGRect
    /// Full main-screen height, for flipping top-left shape boxes to AppKit coords.
    private let screenHeight: CGFloat
    private var model: OverlayModel?
    private var cardsCancellable: AnyCancellable?
    private var permissionsCancellable: AnyCancellable?
    private var shapesCancellable: AnyCancellable?
    /// Height measured just before the last drop; guards against a stale layout pass.
    private var lastDroppedHeight: CGFloat = 0

    init() {
        screenFrame = NSScreen.main?.visibleFrame ?? .zero
        screenHeight = NSScreen.main?.frame.height ?? 0
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
        self.model = model
        contentView = NSHostingView(rootView: CardColumnView(model: model))
        // @Published fires before the value lands: measure on the next main-loop turn.
        let refit = { [weak self] (_: Any) in DispatchQueue.main.async { self?.fit() } }
        cardsCancellable = model.$cards.sink(receiveValue: refit)
        permissionsCancellable = model.$missingPermissions.sink(receiveValue: refit)
        shapesCancellable = model.$shapes.throttle(for: .milliseconds(100), scheduler: DispatchQueue.main, latest: true).sink(receiveValue: refit)
        fit()
    }

    /// Resize to the content's fittingSize; hidden when empty. Drops the oldest
    /// cards (one per pass) until the stack fits above the orb zone, and sits at
    /// the top-left corner while a shape box intersects the top-right frame.
    private func fit() {
        guard let model else { return }
        if model.cards.isEmpty && model.missingPermissions.isEmpty {
            orderOut(nil)
            return
        }
        guard let content = contentView else { return }
        let maxHeight = screenFrame.height - 120
        let size = content.fittingSize
        if size.height > maxHeight, !model.cards.isEmpty, size.height != lastDroppedHeight {
            lastDroppedHeight = size.height
            model.dropOldestCard()  // publish re-triggers fit()
            return
        }
        lastDroppedHeight = 0
        let clamped = CGSize(width: size.width, height: min(size.height, maxHeight))
        let topLeft = Self.shapeIntersects(shapes: model.shapes,
                                           panel: panelFrame(size: clamped, topLeft: false),
                                           screenHeight: screenHeight)
        setFrame(panelFrame(size: clamped, topLeft: topLeft), display: false)
        orderFrontRegardless()
    }

    private func panelFrame(size: CGSize, topLeft: Bool) -> NSRect {
        NSRect(x: topLeft ? screenFrame.minX + 16 : screenFrame.maxX - 16 - size.width,
               y: screenFrame.maxY - 16 - size.height,
               width: size.width,
               height: size.height)
    }

    /// Shape boxes are top-left-origin screen points; AppKit frames are bottom-left.
    private static func shapeIntersects(shapes: [HUDShape], panel: NSRect, screenHeight: CGFloat) -> Bool {
        guard screenHeight > 0 else { return false }
        return shapes.contains { shape in
            let r = shape.rect
            let flipped = CGRect(x: r.minX, y: screenHeight - r.maxY, width: r.width, height: r.height)
            return flipped.intersects(panel)
        }
    }
}
