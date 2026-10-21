import AVFoundation
import Foundation
import UserNotifications

/// Background-audio keepalive (DESIGN.md "Components", ios row; "Failure
/// handling", iPhone WS drops row): a silent looping player holds the audio
/// session — and with it the gateway WS — alive while the app is backgrounded.
/// Also schedules the local notifications that surface handoffs (no APNs; the
/// WhatsApp/iMessage `jarvis://handoff/<id>` link is the out-of-app ping, this
/// is the in-app fallback).
enum KeepAlive {
    private static var player: AVAudioPlayer?
    private static var observer: NSObjectProtocol?
    /// Mixes with other apps' audio: music (Spotify) neither interrupts the
    /// keepalive (which froze the app and silenced session alerts) nor is
    /// stopped by Jarvis.
    static let options: AVAudioSession.CategoryOptions = [.mixWithOthers]

    static var isRunning: Bool { player?.isPlaying == true }

    /// Called at launch and on every foreground: a call, Siri or a route change
    /// stops the player, and nothing else would start it again. Activation is
    /// most reliable foregrounded; an interruption that ends retries here too.
    static func start() {
        guard !isRunning else { return }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default, options: options)
            try session.setActive(true)
            if player == nil {
                player = try AVAudioPlayer(data: silentWav(seconds: 1))
                player?.numberOfLoops = -1
                player?.volume = 0
            }
            player?.play()
        } catch {
            NSLog("KeepAlive: audio session failed: %@", error.localizedDescription)
        }
        if observer == nil {
            observer = NotificationCenter.default.addObserver(
                forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
            ) { note in
                let ended = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt)
                    == AVAudioSession.InterruptionType.ended.rawValue
                if ended { KeepAlive.start() }
            }
        }
    }

    static func stop() {
        player?.stop()
        player = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Dictation coordination (Phase 0 spec, behavior item 10): while the mic
    /// is held the shared session must record AND keep playing (the keepalive
    /// player must not die mid-hold), so it flips to playAndRecord routed to
    /// the speaker. The silent player keeps running through the switch.
    static func beginListening() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
        try? session.setActive(true)
    }

    /// Restore exactly what start() configured above: playback category,
    /// default mode, mixable, active. The silent player is never stopped,
    /// so the background WS keepalive survives a listen/stop cycle intact.
    static func endListening() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default, options: options)
        try? session.setActive(true)
        if !isRunning { player?.play() }
    }

    /// Fire-and-forget permission ask (called once on first foreground launch).
    static func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    /// Immediate local notification for a handoff push that landed while the
    /// app was backgrounded.
    static func notifyHandoff(id: String) {
        let content = UNMutableNotificationContent()
        content.title = "Jarvis handoff"
        content.body = "Open the Jarvis app to continue on your phone (\(id))."
        content.sound = .default
        let request = UNNotificationRequest(identifier: "handoff-\(id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    /// Smallest valid silent WAV: 16-bit mono 8000 Hz PCM, all-zero samples.
    private static func silentWav(seconds: Double) -> Data {
        let sampleRate = 8000
        let samples = Int(Double(sampleRate) * seconds)
        var data = Data()
        func le32(_ value: UInt32) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        func le16(_ value: UInt16) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        le32(0x46464952)               // "RIFF"
        le32(UInt32(36 + samples * 2)) // riff chunk size
        le32(0x45564157)               // "WAVE"
        le32(0x20746d66)               // "fmt "
        le32(16)                       // fmt chunk size
        le16(1)                        // PCM
        le16(1)                        // mono
        le32(UInt32(sampleRate))
        le32(UInt32(sampleRate * 2))   // byte rate
        le16(2)                        // block align
        le16(16)                       // bits per sample
        le32(0x61746164)               // "data"
        le32(UInt32(samples * 2))
        data.append(Data(count: samples * 2))
        return data
    }
}
