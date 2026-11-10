import AppKit
import SwiftUI
import OverlayCore

/// Full-screen HUD: shapes at their screen boxes, orb bottom-right, status banner.
@MainActor
struct OverlayRootView: View {
    @ObservedObject var model: OverlayModel

    var body: some View {
        ZStack(alignment: .topLeading) {
            // Each ShapeView fills the stack and positions itself at its box.
            ForEach(Array(model.shapes.enumerated()), id: \.offset) { _, shape in
                ShapeView(shape: shape)
            }
            VStack(spacing: 12) {
                Spacer()
                if let banner = model.banner {
                    Text(banner)
                        .font(.system(size: 13))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(Color.black.opacity(0.8), in: Capsule())
                        .accessibilityLabel(banner)
                }
                HStack {
                    Spacer()
                    OrbView(state: model.orb, level: model.level)
                        .padding(24)
                }
            }
            // The panel spans the whole screen (shape boxes are screen points), but the Dock
            // draws over its bottom/right edge: keep the orb inside the visible frame.
            .padding(.bottom, Self.dockInset.bottom)
            .padding(.trailing, Self.dockInset.trailing)
        }
    }

    private static var dockInset: (bottom: CGFloat, trailing: CGFloat) {
        guard let screen = NSScreen.main else { return (0, 0) }
        return (max(0, screen.visibleFrame.minY - screen.frame.minY),
                max(0, screen.frame.maxX - screen.visibleFrame.maxX))
    }
}
