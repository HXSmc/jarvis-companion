import AppKit

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
// Top-level code runs on the main thread; say so for the @MainActor delegate.
let delegate = MainActor.assumeIsolated { AppDelegate() }
app.delegate = delegate
app.run()
