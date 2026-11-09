import SwiftUI
import AppKit

/// One accent, dark translucent surfaces, mono micro-labels (HUD reference video look).
public enum Theme {
    /// The single accent (red, #FF3B30 family).
    public static let accent = Color(red: 1, green: 59 / 255, blue: 48 / 255)
    public static let cardBackground = Color.black.opacity(0.72)
    public static let cardBorder = Color.white.opacity(0.18)
    public static let microLabel = Font.system(size: 10, weight: .medium, design: .monospaced)
    public static let cardTitle = Font.system(size: 17, weight: .bold)

    /// Respect the user's Reduce Motion setting (no pulse/rotation when on).
    public static var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }
}
