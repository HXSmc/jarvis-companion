import BackgroundTasks
import Foundation
import UserNotifications
import UIKit

/// J3 build item 1: local notification for new vendor inbox mail, mirroring
/// KeepAlive.notifyHandoff's UNMutableNotificationContent pattern. The tap
/// routes through the same `openHandoff` path that handles `jarvis://inbox`
/// (the delegate carries the url in userInfo and AppModel re-uses its handler).
enum InboxNotifier {
    /// J3 build item 2: BGTaskScheduler identifier (Info.plist
    /// BGTaskSchedulerPermittedIdentifiers must list the same string).
    static let refreshTaskID = "personal.jarvis.inbox-refresh"

    /// "Jarvis inbox" / "<n> new vendor email(s) — tap to open" with the
    /// jarvis://inbox url in userInfo. Fired by AppModel's `inbox_new`
    /// handler ONLY while backgrounded (`!web.isForeground`, the same guard
    /// the `navigate` case uses) — in the foreground the badge/sheet refresh
    /// is the notification, per iOS conventions.
    static func notify(unread: Int, new: Int) {
        guard new > 0 else { return }
        let content = UNMutableNotificationContent()
        content.title = "Jarvis inbox"
        content.body = "\(new) new vendor email\(new == 1 ? "" : "s") — tap to open"
        content.sound = .default
        content.userInfo = ["url": "jarvis://inbox"]
        let request = UNNotificationRequest(
            identifier: "inbox-\(UUID().uuidString.prefix(8))",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    /// Gateway `owner_note` (gateway/notify.py): a late answer, or a session
    /// at a crossroads waiting for the owner. Shown even in the foreground
    /// (see the delegate's willPresent); a tap opens the session's chat.
    static func notifyNote(title: String, body: String, sessionID: String?) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.userInfo = ["note": true, "session_id": sessionID ?? "", "title": title]
        let request = UNNotificationRequest(
            identifier: "note-\(UUID().uuidString.prefix(8))",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    /// Register the BGAppRefreshTask handler. MUST run before launch finishes
    /// (App.init); the closure form is required — the delegate-based variant
    /// is unavailable on this deployment target. The handler refreshes the
    /// inbox, notifies on any unread delta (graceful no-op when the WS/audio
    /// keepalive already delivered), reschedules, and always completes.
    static func registerBackgroundRefresh(model: AppModel) {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshTaskID, using: nil) { task in
            guard let refresh = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            DispatchQueue.main.async {
                runRefresh(refresh, model: model)
            }
        }
    }

    /// Submit the next refresh request; earliest 15 min out. `try?` — an
    /// unsupported environment (iPad sidecar, simulator quirks) is a no-op,
    /// never a crash.
    static func scheduleBackgroundRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private static func runRefresh(_ task: BGAppRefreshTask, model: AppModel) {
        scheduleBackgroundRefresh()
        let finished = { task.setTaskCompleted(success: true) }
        task.expirationHandler = { task.setTaskCompleted(success: false) }
        let before = model.inbox.unread
        model.inbox.refresh {
            let delta = model.inbox.unread - before
            if delta > 0 {
                notify(unread: model.inbox.unread, new: delta)
            }
            finished()
        }
    }
}

/// Tap plumbing (was missing app-wide): without a UNUserNotificationCenterDelegate
/// iOS drops foreground presentations AND cold-start taps silently. `didReceive`
/// routes a `jarvis://inbox` tap through AppModel.openHandoff — the exact same
/// path the custom url scheme uses, so the sheet opens from both entry points.
final class InboxNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    /// Set by AppModel.start(); hops to main before touching published state.
    var onOpenInbox: (() -> Void)?
    /// Note tap: session id ("" = none) and the note title.
    var onOpenNote: ((String, String) -> Void)?

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        // Owner notes always show (the owner asked for a notification even
        // with the app open). Inbox delivery stays silent in the foreground
        // (InboxNotifier.notify is only fired while backgrounded).
        if notification.request.content.userInfo["note"] as? Bool == true {
            completionHandler([.banner, .sound])
        } else {
            completionHandler([])
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let info = response.notification.request.content.userInfo
        if info["note"] as? Bool == true {
            let sid = info["session_id"] as? String ?? ""
            let title = info["title"] as? String ?? "Chat"
            DispatchQueue.main.async { [weak self] in
                self?.onOpenNote?(sid, title)
            }
        } else if (info["url"] as? String) == "jarvis://inbox" {
            DispatchQueue.main.async { [weak self] in
                self?.onOpenInbox?()
            }
        }
        completionHandler()
    }
}
