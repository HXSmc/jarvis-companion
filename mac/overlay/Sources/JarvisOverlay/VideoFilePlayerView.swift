import SwiftUI
import AVKit
import AVFoundation
import Combine
import OverlayCore

/// Direct https video file player (group B) inside a `PlayerHostView`.
// shortcut: AVPlayer follows redirects without HostGuard; route through a resource loader if
// direct video URLs ever come from untrusted pages at scale.
struct VideoFilePlayerView: NSViewRepresentable {
    let url: URL
    let key: String

    func makeCoordinator() -> Coordinator {
        Coordinator(key: key)
    }

    func makeNSView(context: Context) -> PlayerHostView {
        let host = PlayerHostView()
        let coordinator = context.coordinator
        MediaPlaybackController.shared.attach(host, key: key)
        coordinator.start(url: url, host: host)
        host.onTearDown = { [weak coordinator] in coordinator?.tearDown() }
        return host
    }

    static func dismantleNSView(_ nsView: PlayerHostView, coordinator: Coordinator) {
        nsView.tearDown()
    }

    func updateNSView(_ nsView: PlayerHostView, context: Context) {}
}

extension VideoFilePlayerView {
    @MainActor
    final class Coordinator {
        private let key: String
        private var task: Task<Void, Never>?
        private var statusCancellable: AnyCancellable?
        private var player: AVPlayer?
        private var playerView: AVPlayerView?
        private var tornDown = false

        init(key: String) {
            self.key = key
        }

        /// HostGuard runs BEFORE the asset exists; a torn-down host creates nothing.
        func start(url: URL, host: PlayerHostView) {
            let key = self.key
            task = Task { [weak self] in
                guard url.scheme == "https", let hostname = url.host,
                      await MediaLoader.hostIsPublic(hostname) else {
                    MediaPlaybackController.shared.failed(key: key)
                    return
                }
                guard let self, !self.tornDown else { return }
                self.buildPlayer(url: url, host: host)
            }
        }

        /// The card's play click was the gesture, so playback starts right away.
        private func buildPlayer(url: URL, host: PlayerHostView) {
            // No shared cookie storage: the player gets an empty jar.
            let asset = AVURLAsset(url: url, options: [AVURLAssetHTTPCookiesKey: [HTTPCookie]()])
            let item = AVPlayerItem(asset: asset)
            let player = AVPlayer(playerItem: item)
            let playerView = AVPlayerView(frame: host.bounds)
            playerView.controlsStyle = .inline
            playerView.autoresizingMask = [.width, .height]
            playerView.player = player
            host.addSubview(playerView)
            self.player = player
            self.playerView = playerView
            statusCancellable = item.publisher(for: \.status)
                .sink { [weak self] status in
                    guard status == .failed else { return }
                    Task { @MainActor in self?.fail() }
                }
            player.play()
        }

        /// Idempotent; stops playback and clears everything the task created.
        func tearDown() {
            guard !tornDown else { return }
            tornDown = true
            task?.cancel()
            task = nil
            statusCancellable?.cancel()
            statusCancellable = nil
            player?.pause()
            player?.replaceCurrentItem(with: nil)
            player = nil
            playerView?.player = nil
            playerView?.removeFromSuperview()
            playerView = nil
        }

        private func fail() {
            MediaPlaybackController.shared.failed(key: key)
        }
    }
}
