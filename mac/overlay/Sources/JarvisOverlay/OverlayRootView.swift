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
        }
    }
}
