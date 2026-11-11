import SwiftUI
import AppKit

/// One accent, dark translucent surfaces, mono micro-labels (HUD reference video look).
public enum Theme {
    /// The single accent (red, #FF3B30 family).
    public static let accent = Color(red: 1, green: 59 / 255, blue: 48 / 255)
    /// 0.86 black so card text keeps contrast over a bright desktop window.
    public static let cardBackground = Color.black.opacity(0.86)
    public static let cardBorder = Color.white.opacity(0.18)

    // Type scale (micro 11, small 12, body 13, title 17, display 44) and radii.
    public static let fontMicro = Font.system(size: 11, weight: .medium, design: .monospaced)
    public static let fontSmall = Font.system(size: 12)
    public static let fontBody = Font.system(size: 13)
    public static let fontTitle = Font.system(size: 17, weight: .bold)
    public static let fontDisplay = Font.system(size: 44, weight: .bold).monospacedDigit()
    public static let radiusCard: CGFloat = 4
    public static let radiusShape: CGFloat = 6

    /// Aliases kept for existing callers.
    public static let microLabel = fontMicro
    public static let cardTitle = fontTitle

    /// The one duration for fades and short overlay transitions (Reduce Motion uses 0).
    public static let motionShort: TimeInterval = 0.2

    /// Respect the user's Reduce Motion setting (no pulse/rotation when on).
    public static var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }
}
