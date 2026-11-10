import SwiftUI

/// Assistant presence orb (44 pt, bottom-right). Pulse/rotation stop when the
/// user has Reduce Motion on.
public enum OrbState: String, CaseIterable {
    /// followUp: the quiet open-mic window after a reply (no wake word needed).
    case idle, listening, followUp, thinking, speaking, error
}

@MainActor
public struct OrbView: View {
    public let state: OrbState
    /// 0...1 audio level, drives the speaking ring scale.
    public var level: Double

    public init(state: OrbState, level: Double = 0) {
        self.state = state
        self.level = level
    }

    @State private var spin = false

    public var body: some View {
        ZStack {
            // Dark disc so the orb reads on a light desktop in every state.
            Circle().fill(Color.black.opacity(0.6))
            switch state {
            case .idle:
                Circle().stroke(Color.white.opacity(0.6), lineWidth: 3)
            case .listening:
                Circle().stroke(Theme.accent, lineWidth: 3)
                    .modifier(Pulse())
            case .followUp:
                Circle().stroke(Theme.accent.opacity(0.5), lineWidth: 2)
            case .thinking:
                Circle()
                    .trim(from: 0, to: 0.25)
                    .stroke(Theme.accent, style: StrokeStyle(lineWidth: 3, lineCap: .round))
                    .rotationEffect(.degrees(spin ? 360 : 0))
                    .onAppear { startSpin() }
            case .speaking:
                Circle().stroke(Theme.accent, lineWidth: 3)
                    .scaleEffect(1 + 0.2 * CGFloat(min(max(level, 0), 1)))
                Circle().fill(Theme.accent.opacity(0.35))
                    .scaleEffect(0.4 + 0.5 * CGFloat(min(max(level, 0), 1)))
            case .error:
                Circle().fill(Theme.accent)
                Text("!")
                    .font(Theme.fontTitle)
                    .foregroundStyle(.white)
            }
        }
        .frame(width: 44, height: 44)
        .animation(Theme.reduceMotion ? nil : .easeOut(duration: 0.2), value: state)
        .accessibilityLabel(axLabel)
    }

    private var axLabel: String {
        switch state {
        case .idle: return "Jarvis idle"
        case .listening: return "Jarvis listening"
        case .followUp: return "Jarvis listening for a follow-up"
        case .thinking: return "Jarvis thinking"
        case .speaking: return "Jarvis speaking"
        case .error: return "Jarvis needs attention"
        }
    }

    private func startSpin() {
        guard !Theme.reduceMotion else { return }
        withAnimation(.linear(duration: 1.2).repeatForever(autoreverses: false)) {
            spin = true
        }
    }
}

@MainActor
private struct Pulse: ViewModifier {
    @State private var on = false

    func body(content: Content) -> some View {
        content
            .scaleEffect(on ? 1.15 : 1.0)
            .opacity(on ? 1.0 : 0.7)
            .onAppear {
                guard !Theme.reduceMotion else { return }
                withAnimation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true)) {
                    on = true
                }
            }
    }
}
