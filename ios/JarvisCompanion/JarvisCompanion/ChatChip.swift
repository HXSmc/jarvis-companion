import Foundation

/// Owner-message chip state machine for session chats. The router labels a
/// message at birth (`queued` / `delivered` / `resumed`) and drains queued
/// messages strictly FIFO, one per turn-end; SSE events carry no message id,
/// so each entry skip-counts the `done`s of turns it was not part of. Every
/// transition is driven by a real SSE event — no timers, no guessing.
struct ChatChipMachine {
    /// A live SSE event, as seen by the chat stream.
    enum Event {
        /// A `status` or `text` event: some turn is doing work.
        case activity
        /// A turn finished normally.
        case turnDone
        /// A turn stopped to ask the owner something.
        case turnNeedsInput
        /// A turn failed (an error line is rendered separately; chips stay).
        case turnError
    }

    /// A chip change to write back to the owner line.
    struct Change: Equatable {
        let lineIndex: Int
        let chip: String
    }

    /// One tracked send: its owner line, current chip, and how many more
    /// `done`s close turns this message was not part of.
    private struct Entry {
        let lineIndex: Int
        var chip: String
        var skipDones: Int
    }

    private var entries: [Entry] = []

    /// Track a just-delivered message. `action` is the POST response's action
    /// (the birth chip); `sentMidTurn` means a turn was already running, so
    /// the message queues behind it and every earlier still-pending one.
    mutating func track(lineIndex: Int, action: String, sentMidTurn: Bool) {
        entries.append(Entry(
            lineIndex: lineIndex,
            chip: action,
            skipDones: sentMidTurn ? 1 + entries.count : 0
        ))
    }

    /// Apply one SSE event; returns the chips that changed.
    mutating func apply(_ event: Event) -> [Change] {
        switch event {
        case .activity:
            // The next unskipped, not-yet-working message is being answered.
            guard let index = entries.firstIndex(where: {
                $0.skipDones == 0 && ["queued", "delivered", "resumed"].contains($0.chip)
            }) else { return [] }
            entries[index].chip = "working"
            return [Change(lineIndex: entries[index].lineIndex, chip: "working")]
        case .turnDone:
            // Every queued message moves one turn closer; independently, the
            // working message was answered by this done.
            for index in entries.indices where entries[index].skipDones > 0 {
                entries[index].skipDones -= 1
            }
            guard let index = entries.firstIndex(where: { $0.chip == "working" }) else {
                return []
            }
            let change = Change(lineIndex: entries[index].lineIndex, chip: "answered")
            entries.remove(at: index)
            return [change]
        case .turnNeedsInput:
            guard let index = entries.firstIndex(where: { $0.chip == "working" }) else {
                return []
            }
            let change = Change(lineIndex: entries[index].lineIndex, chip: "needs_input")
            entries.remove(at: index)
            return [change]
        case .turnError:
            // No chip change, but the failed turn is over: its working entry
            // leaves tracking so a later done cannot answer it.
            if let index = entries.firstIndex(where: { $0.chip == "working" }) {
                entries.remove(at: index)
            }
            return []
        }
    }
}
