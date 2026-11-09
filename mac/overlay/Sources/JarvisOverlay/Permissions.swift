import AVFoundation
import ApplicationServices
import CoreGraphics
import Foundation
import Speech

enum Permission: String, CaseIterable {
    case microphone
    case speech
    case accessibility
    case screenRecording
}

enum Permissions {
    static func missing() -> [Permission] {
        var out: [Permission] = []
        if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized { out.append(.microphone) }
        if SFSpeechRecognizer.authorizationStatus() != .authorized { out.append(.speech) }
        if !AXIsProcessTrusted() { out.append(.accessibility) }
        if !CGPreflightScreenCaptureAccess() { out.append(.screenRecording) }
        return out
    }

    /// Accessibility and Screen Recording prompts are system-modal; the return
    /// value is the CURRENT state (the grant itself lands after the user acts).
    static func request(_ p: Permission) async -> Bool {
        switch p {
        case .microphone:
            await withCheckedContinuation { c in
                AVCaptureDevice.requestAccess(for: .audio) { c.resume(returning: $0) }
            }
        case .speech:
            await withCheckedContinuation { c in
                SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0 == .authorized) }
            }
        case .accessibility:
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
            return AXIsProcessTrustedWithOptions(options)
        case .screenRecording:
            return CGRequestScreenCaptureAccess()
        }
    }

    static func settingsURL(_ p: Permission) -> URL {
        let base = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
        switch p {
        case .microphone: return URL(string: base + "Microphone")!
        case .speech: return URL(string: base + "SpeechRecognition")!
        case .accessibility: return URL(string: base + "Accessibility")!
        case .screenRecording: return URL(string: base + "ScreenCapture")!
        }
    }

    static func paneName(_ p: Permission) -> String {
        switch p {
        case .microphone: return "Microphone"
        case .speech: return "Speech Recognition"
        case .accessibility: return "Accessibility"
        case .screenRecording: return "Screen Recording"
        }
    }
}
