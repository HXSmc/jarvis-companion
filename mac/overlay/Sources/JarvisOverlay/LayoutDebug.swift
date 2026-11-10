import Foundation
import OverlayCore

/// Live-test trigger for the card layout: a distributed notification whose object string is one
/// of the fixed LayoutDebugCommand strings. Exists only when the app was started with the
/// layout debug flag (startIfEnabled); otherwise no observer is ever registered. The
/// notification carries no card text: demo cards are built-in samples.
@MainActor
final class LayoutDebug: NSObject {
    private static let name = Notification.Name("com.jarvis.overlay.debug.layout")
    private let perform: @MainActor (LayoutDebugCommand, String) -> Void

    private init(perform: @escaping @MainActor (LayoutDebugCommand, String) -> Void) {
        self.perform = perform
        super.init()
    }

    deinit {
        DistributedNotificationCenter.default().removeObserver(self)
    }

    /// The observer, or nil (and nothing registered) when the flag is off.
    static func startIfEnabled(perform: @escaping @MainActor (LayoutDebugCommand, String) -> Void) -> LayoutDebug? {
        guard ProcessInfo.processInfo.environment["JARVIS_OVERLAY_LAYOUT_DEBUG"] == "1" else { return nil }
        let debug = LayoutDebug(perform: perform)
        DistributedNotificationCenter.default().addObserver(debug, selector: #selector(received(_:)),
                                                            name: name, object: nil,
                                                            suspensionBehavior: .deliverImmediately)
        return debug
    }

    @objc private func received(_ note: Notification) {
        guard let text = note.object as? String, let command = LayoutDebugCommand.parse(text) else { return }
        perform(command, text)
    }
}
