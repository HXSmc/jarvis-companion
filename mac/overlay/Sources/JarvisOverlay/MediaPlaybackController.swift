import AppKit
import Combine
import OverlayCore

/// The one live inline player (YouTube web view or AVPlayerView) and its single teardown path.
@MainActor
final class MediaPlaybackController: ObservableObject {
    static let shared = MediaPlaybackController()

    @Published private(set) var playingKey: String?
    @Published private(set) var failedKeys: Set<String> = []
    /// Click time of the current player; nil when nothing plays.
    private(set) var playingSince: Date?
    /// The one live player container; weak because the card's panel owns the view hierarchy.
    private weak var host: PlayerHostView?

    func play(key: String) {
        stopAll()
        failedKeys.remove(key)
        playingKey = key
        playingSince = Date()
    }

    /// THE teardown path: the live host's tearDown() runs synchronously here, so sound stops
    /// in the same main-loop turn, before any state is cleared.
    func stopAll() {
        host?.tearDown()
        host = nil
        playingKey = nil
        playingSince = nil
    }

    func failed(key: String) {
        guard key == playingKey else { return }
        stopAll()
        failedKeys.insert(key)
    }

    /// Registers the live player view; attaching while another key plays tears the newcomer
    /// down at once, and a new host for the playing key replaces (and tears down) the old one.
    func attach(_ host: PlayerHostView, key: String) {
        // A stale view (its card no longer playing) must never stop the live player.
        guard key == playingKey else {
            host.tearDown()
            return
        }
        if let current = self.host, current !== host { current.tearDown() }
        self.host = host
    }

    /// stopAll() when the playing card left the list or its content changed; failures of cards
    /// that are gone go with them.
    func cardsChanged(old: [HUDCard], new: [HUDCard]) {
        if let playingKey {
            let oldCard = old.first { $0.listKey == playingKey }
            let newCard = new.first { $0.listKey == playingKey }
            if newCard == nil || newCard != oldCard { stopAll() }
        }
        failedKeys.formIntersection(Set(new.map(\.listKey)))
    }

    func phase(for key: String) -> VideoPhase {
        if key == playingKey { return .playing }
        return failedKeys.contains(key) ? .failed : .idle
    }
}

/// Container every player view lives in; HUDWindow lets presses inside this view reach the
/// player untouched (no drag, no re-ordered events).
final class PlayerHostView: NSView {
    var onTearDown: (@MainActor () -> Void)?

    /// Runs onTearDown once (then nil), removes all subviews. Idempotent.
    func tearDown() {
        onTearDown?()
        onTearDown = nil
        subviews.forEach { $0.removeFromSuperview() }
    }
}
