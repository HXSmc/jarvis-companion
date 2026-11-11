import Foundation

/// Activation sound played once when the wake word is heard ("I am listening").
///
/// Config: env var `JARVIS_OVERLAY_WAKE_SOUND`, read once on the first wake, so a change
/// needs an app restart. Unset or empty plays the macOS system sound `Tink`. `0`, `off`,
/// `none` or `false` (any case) turns it off. Any other value is used as a system sound
/// name (letters, digits, space, `_`, `-`, up to 32 chars); anything else falls back to `Tink`.
/// The value is never treated as a path or URL.
public enum WakeChime {
    public static let envKey = "JARVIS_OVERLAY_WAKE_SOUND"
    public static let defaultName = "Tink"
    /// Quiet on purpose: it repeats on every wake and the mic stays open.
    public static let volume: Float = 0.5

    private static let offValues: Set<String> = ["0", "off", "none", "false"]

    /// The system sound name to play, or nil when the chime is off.
    public static func soundName(env: [String: String]) -> String? {
        guard let raw = env[envKey], !raw.isEmpty else { return defaultName }
        if offValues.contains(raw.lowercased()) { return nil }
        let allowed = raw.range(of: "^[A-Za-z0-9 _-]{1,32}\\z", options: .regularExpression) != nil
        return allowed ? raw : defaultName
    }
}
