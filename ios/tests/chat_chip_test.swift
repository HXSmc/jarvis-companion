import Foundation

// Headless asserts for ChatChipMachine (compiled together with ChatChip.swift
// by run_chat_chip_tests.sh). Exits non-zero on the first failure; "ok" at the
// end means every scenario passed.

var machine = ChatChipMachine()
/// Mirror of the owner lines' chips, updated the way ChatModel would.
var chips: [Int: String] = [:]

/// Track a send; the entry chip starts as the POST action by definition.
func track(_ line: Int, _ action: String, midTurn: Bool) {
    machine.track(lineIndex: line, action: action, sentMidTurn: midTurn)
    chips[line] = action
}

@discardableResult
func apply(_ event: ChatChipMachine.Event) -> [ChatChipMachine.Change] {
    let changes = machine.apply(event)
    for change in changes { chips[change.lineIndex] = change.chip }
    return changes
}

func expect(_ label: String, _ line: Int, _ chip: String) {
    guard chips[line] == chip else {
        FileHandle.standardError.write(Data(
            "FAIL \(label): line \(line) expected \(chip), got \(chips[line] ?? "nil")\n".utf8
        ))
        exit(1)
    }
}

func expectNoChange(_ label: String, _ changes: [ChatChipMachine.Change]) {
    guard changes.isEmpty else {
        FileHandle.standardError.write(Data("FAIL \(label): expected no changes, got \(changes)\n".utf8))
        exit(1)
    }
}

@main
struct ChipTestMain {
    static func main() {
    // a. mid-turn queue: the message queues behind the running turn.
    track(0, "queued", midTurn: true)
    expectNoChange("a: running turn's done does not answer a queued message", apply(.turnDone))
    expect("a: still queued after the earlier turn's done", 0, "queued")
    apply(.activity)
    expect("a: first activity of its own turn -> working", 0, "working")
    apply(.turnDone)
    expect("a: its own turn's done -> answered", 0, "answered")

    // b. immediate follow-up mislabelled queued at birth (today's router folding).
    track(1, "queued", midTurn: false)
    apply(.activity)
    expect("b: activity -> working despite the queued birth label", 1, "working")
    apply(.turnDone)
    expect("b: done -> answered", 1, "answered")

    // c. two messages queued behind one busy turn.
    machine = ChatChipMachine()
    chips = [:]
    track(10, "queued", midTurn: true)  // skipDones 1
    track(11, "queued", midTurn: true)  // skipDones 2
    expectNoChange("c: running turn's done answers neither", apply(.turnDone))
    apply(.activity)
    expect("c: m1 working", 10, "working")
    expect("c: m2 untouched while m1 works", 11, "queued")
    apply(.turnDone)
    expect("c: m1 answered", 10, "answered")
    apply(.activity)
    expect("c: m2 working after m1's done unskipped it", 11, "working")
    apply(.turnDone)
    expect("c: m2 answered", 11, "answered")

    // d. needs_input during the message's own turn.
    track(20, "queued", midTurn: false)
    apply(.activity)
    expect("d: working", 20, "working")
    apply(.turnNeedsInput)
    expect("d: needs_input", 20, "needs_input")

    // e. error events never touch chips (the error line is rendered separately).
    track(30, "queued", midTurn: false)
    apply(.activity)
    expectNoChange("e: turnError changes nothing", apply(.turnError))
    expect("e: still working after an error", 30, "working")

    // f. delivered / resumed birth chips advance the same way.
    track(40, "delivered", midTurn: false)
    apply(.activity)
    expect("f: delivered -> working", 40, "working")
    apply(.turnDone)
    expect("f: -> answered", 40, "answered")
    track(41, "resumed", midTurn: false)
    apply(.activity)
    expect("f: resumed -> working", 41, "working")
    apply(.turnNeedsInput)
    expect("f: -> needs_input", 41, "needs_input")

    // g. untracked events are a no-op (replayed history on a fresh open).
    machine = ChatChipMachine()
    expectNoChange("g: untracked activity", apply(.activity))
    expectNoChange("g: untracked done", apply(.turnDone))

    print("ok")
    }
}
