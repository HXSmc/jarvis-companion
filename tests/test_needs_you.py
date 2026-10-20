"""Replay of real wrong "Needs you" recap lines (2026-10-20) plus the rules that replace them.

Each bad case is the router row + the cached model recap exactly as the phone
got them (router.db / ~/.jarvis/central_state.json). None of those sessions was
stopped on the owner, so none may show a "Needs you" line. A "Needs you" line
comes only from the router's real block, in full, and goes once answered.
"""

from __future__ import annotations

from gateway import central

REAL_GENERATE = central._generate_plain  # captured before conftest stubs it

CD3B9DFC_RECAP = [  # TikTok round 3: asks came from a clipped, already-answered turn + a queued hub message
    "I checked the posting schedule on the Mac against the saved copy.",
    "I cleared out a temporary holding folder I had made.",
    "I'm holding off on building the other 14 posts until you approve the new style.",
    "Needs you: Tell me whether to start the third round now or wait for the November 18 results, "
    "and whether to add the item my notes cut off.",
]
AE8315FB_RECAP = [  # exited Mac session; model line also cut mid-word ("po…")
    "I generated more videos in the same taste and style as before.",
    "I built a new posting schedule for them.",
    "I handed the video list and schedule to the TikTok session for review.",
    "Needs you: Run /clear to empty this full chat, then approve or change the new video list "
    "and schedule on the TikTok card before anything po…",
]
CA85E8BF_RECAP = [  # idle C++ tutoring chat; the ask was invented
    "I set up the test environment on the mini PC.",
    "I made it so each test starts fresh, runs, and then cleans itself up.",
    "I wrote instructions for running a test from your project folder on the Mac.",
    "Needs you: run a test from your project folder on the Mac to check it works.",
]

BAD_CASES = [
    # (router row as GET /sessions sends it, cached recap)
    ({"session_id": "cd3b9dfc-da6b-461e-888e-beb93246d262", "state": "running", "phase": "in_progress",
      "lifecycle": "working", "health": "working",
      "task": "Follow-up from Mac hub (fable 2): the OWNER wants to approve the new round-2 odd-day style NOW, "
              "before you build all 14."}, CD3B9DFC_RECAP),
    # the same session after its process exited at ~17:11Z with the hub messages still queued
    ({"session_id": "cd3b9dfc-da6b-461e-888e-beb93246d262", "state": "done", "phase": "done",
      "lifecycle": "exited", "health": "done"}, CD3B9DFC_RECAP),
    ({"session_id": "ae8315fb", "state": "done", "phase": "done", "lifecycle": "exited", "health": "done",
      "device": "Mac"}, AE8315FB_RECAP),
    ({"session_id": "ca85e8bf", "state": "idle", "phase": "done", "lifecycle": "idle", "health": "idle",
      "device": "Mac"}, CA85E8BF_RECAP),
    # watchdog "attention" is the router's guess, not the session asking
    ({"session_id": "s-att", "state": "needs_input", "phase": "in_review", "lifecycle": "attention",
      "health": "attention", "attention_reason": "ended without DONE"}, CD3B9DFC_RECAP),
]


def _render(row: dict, recap: list[str]) -> dict:
    plain = {row["session_id"]: {"hash": "x", "doing": "d", "recap": recap}}
    return central._session(row, plain)


def _asks(session: dict) -> list[str]:
    return [r for r in session["recap"] if r.startswith("Needs you")]


def test_replayed_bad_cases_show_no_needs_you():
    for row, recap in BAD_CASES:
        out = _render(row, recap)
        assert _asks(out) == [], (row["session_id"], out["recap"])
        assert len(out["recap"]) == len(recap) - 1  # the other bullets stay


def test_model_prompt_never_sees_task_or_asks_for_needs_you(monkeypatch):
    assert "Needs you" not in central.PLAIN_INSTRUCTION
    seen = {}
    monkeypatch.setattr(central, "_run_claude_plain", lambda prompt, _t: seen.setdefault("p", prompt) and None)
    REAL_GENERATE({"title": "T", "task": "HUB QUEUED TEXT", "state": "running", "summary": "s"})
    assert "HUB QUEUED TEXT" not in seen["p"]


def test_real_question_shows_in_full_and_clears_once_answered():
    long_q = "Should I add the 'wait for internet' fix to the Mac sender, " + "and also " * 30 + "keep the 17:30 slot?"
    row = {"session_id": "s1", "state": "needs_input", "phase": "in_review", "lifecycle": "needs_input",
           "health": "waiting", "needs_prompt": long_q}
    assert _asks(_render(row, CD3B9DFC_RECAP)) == [f"Needs you: {' '.join(long_q.split())}"]  # never cut
    answered = {**row, "state": "running", "phase": "in_progress", "lifecycle": "working", "health": "working",
                "needs_prompt": None}
    assert _asks(_render(answered, CD3B9DFC_RECAP)) == []


def test_blocked_turn_uses_router_reason_and_no_cache_still_works():
    row = {"session_id": "s2", "state": "needs_input", "lifecycle": "blocked",
           "blocked_reason": "need the NoteKit login to continue"}
    assert _asks(central._session(row, {})) == ["Needs you: need the NoteKit login to continue"]
    still_live = {**row, "state": "running"}  # blocked but its turn is still running
    assert _asks(central._session(still_live, {})) == []
    bare = {"session_id": "s3", "state": "needs_input"}  # older router: no lifecycle / prompt
    assert _asks(central._session(bare, {})) == ["Needs you: it is waiting for your answer in the chat."]
