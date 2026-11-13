import CoreAudio

/// Facts about the default output route, used to decide whether barge-in may open:
/// only routes whose speakers sit close to the user's mic (headphones) get voice-triggered barge-in.
public enum AudioRoute {
    /// True for Bluetooth and Bluetooth LE devices, and for built-in output whose data source is
    /// the headphone jack ('hdpn'). Everything else (speakers, HDMI, USB, AirPlay, ...) is false,
    /// so an unknown route fails safe: no barge-in.
    public static func isHeadphones(transport: UInt32, dataSource: UInt32?) -> Bool {
        switch transport {
        case UInt32(kAudioDeviceTransportTypeBluetooth), UInt32(kAudioDeviceTransportTypeBluetoothLE):
            return true
        case UInt32(kAudioDeviceTransportTypeBuiltIn):
            return dataSource == 0x6864706E // 'hdpn'
        default:
            return false
        }
    }
}

/// How barge-in may open, as told by the service's /health.
public enum BargeMode: String {
    case off
    case headphones
    case aec

    /// The explicit `barge_mode` string wins when valid; otherwise the older booleans decide
    /// (aec needs bargeAec true and bargeIn not explicitly false; plain bargeIn is headphones).
    public static func from(health mode: String?, bargeIn: Bool?, bargeAec: Bool?) -> BargeMode {
        if let mode = mode, let parsed = BargeMode(rawValue: mode) { return parsed }
        if bargeAec == true, bargeIn != false { return .aec }
        if bargeIn == true { return .headphones }
        return .off
    }
}
