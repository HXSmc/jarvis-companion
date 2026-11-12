"""Round 3 (owner, 02:18-02:19 debug page):

(a) "Can you show me the last, uh" ended by endOfSpeech -> Jarvis asked "your request seems to
    have been cut off" and ignored the rest. A trailing-off utterance must keep listening and
    the follow-up (no wake word) must continue the same sentence.
(b) "highlight for me the last debugging session" went to the brain with no screen context
    while the "Jarvis voice debug" Safari window was on screen. Visual verbs must look at the
    screen (windows + text) and draw a shape on what the user means.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_overlay_service import (  # noqa: E402  (shared stubs + fixtures)
    AUTH_HEADER, POOL_TEXTS, FakeConversations, client, make_deps, reset_overlay_state, says, turn,
)
from gateway import overlay  # noqa: E402
from mac.voice.intents import join_continuation, looks_incomplete, parse  # noqa: E402

__all__ = ["client", "reset_overlay_state"]  # pytest fixtures re-exported

# What the app sent at 02:19:01: Safari showing the overlay's own debug page, Terminal behind it.
DEBUG_SCREEN = {
    "scale": 2.0,
    "app": "Terminal",
    "windows": [
        {"app": "Terminal", "title": "~ — zsh — 80x24", "box": [0, 25, 700, 500]},
        {"app": "Safari", "title": "Jarvis voice debug", "box": [720, 25, 1440, 880]},
    ],
    "ocr": [
        {"text": "Jarvis voice debug", "box": [1500, 120, 1900, 160]},
        {"text": "Can you show me the last, uh", "box": [1500, 300, 2100, 330]},
    ],
}


def test_trailing_off_detection():
    for text in ("Can you show me the last, uh", "Can you show me the last, huh", "the last, huh?", "show me the", "open the", "what is two plus",
                 "tell me about my", "um", "and the,"):
        assert looks_incomplete(text), text
    for text in ("Can you show me the last debugging session", "what time is it",
                 "Tell me a joke.", "open Safari", "Jarvis", ""):
        assert not looks_incomplete(text), text
    assert (join_continuation("Can you show me the last, uh", "debugging session")
            == "Can you show me the last debugging session")


def test_cut_off_turn_waits_then_follow_up_continues_it(client):
    """(a): no "you were cut off" line, no brain call; the follow-up completes the sentence."""
    pool = FakeConversations(chunks=['{"say": "Here is the last session."}\n'])
    overlay.set_deps(make_deps(conversations=pool, quiet_conversations=pool))
    first = turn(client, "Can you show me the last, uh", conv_id="a1")
    assert says(first) == [] and [e["type"] for e in first] == ["done"]
    assert pool.sessions == {}
    turn(client, "debugging session", conv_id="a1", followup=True)
    prompt = pool.sessions["a1"].prompts[0]
    assert prompt.startswith("Can you show me the last debugging session")
    steps = [s["step"] for t in client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
             for s in t["steps"]]
    assert "incomplete" in steps and "continued" in steps


def test_new_wake_after_a_cut_off_starts_fresh(client):
    pool = FakeConversations(chunks=['{"say": "ok"}\n'])
    overlay.set_deps(make_deps(conversations=pool))
    turn(client, "tell me about my", conv_id="a2")
    turn(client, "what is the capital of France", conv_id="a2")  # wake word, not a follow-up
    assert pool.sessions["a2"].prompts[0].startswith("what is the capital of France")


def test_brain_judges_incomplete_and_keeps_listening(client):
    pool = FakeConversations(chunks=['{"incomplete": true}\n'])
    overlay.set_deps(make_deps(conversations=pool))
    events = turn(client, "could you compare the", conv_id="a3")  # precheck catches this one
    assert says(events) == []
    events = turn(client, "could you compare", conv_id="a4")  # only the brain can tell
    spoken = says(events)
    assert spoken and spoken[0] in POOL_TEXTS  # the pool ack only, never "you were cut off"
    pool.chunks = ['{"say": "Sure."}\n']
    pool.sessions.clear()
    turn(client, "Python and Swift", conv_id="a4", followup=True)
    assert pool.sessions["a4"].prompts[0].startswith("could you compare Python and Swift")
    assert "incomplete" in overlay.ANSWER_PROMPT


def test_visual_verbs_route_to_the_screen():
    for text in ("highlight for me the last debugging session", "circle the newest turn",
                 "Can you show me the last debugging session", "point to the error",
                 "where is the debug page", "show me the last, uh \u2026 debugging session",
                 "highlight the URL of this page", "show me the previous window"):
        assert parse(text, apps={}).kind == "look", text
    # Weak leads (show me / where / find / which) look only at screen nouns or "the last <noun>".
    for text in ("where is Paris", "show me how a transformer works",
                 "show me the weather in Riyadh", "what is the capital of France",
                 "show me the latest news", "find the current price of bitcoin",
                 "show me the top 10 restaurants", "show me this week's forecast",
                 "which is better, this or that", "find a good page on python decorators"):
        assert parse(text, apps={}).kind == "answer", text
    assert parse("highlight the search field", apps={}).kind == "ground"


def test_highlight_the_last_debugging_session_draws_on_the_debug_window(client):
    """(b) fixture: the VLM sees the window titles and picks the Safari debug window."""
    seen = []

    def vlm(prompt, jpeg):
        seen.append(prompt)
        match = re.search(r'\[(\d+)\] window "Safari: Jarvis voice debug"', prompt)
        return '{"id": %s}' % (match.group(1) if match else "null")

    pool = FakeConversations()
    overlay.set_deps(make_deps(vlm=vlm, conversations=pool, quiet_conversations=pool))
    events = turn(client, "highlight for me the last debugging session", conv_id="b1",
                  screen=DEBUG_SCREEN)
    shapes = [e["shape"] for e in events if e["type"] == "shape"]
    assert len(shapes) == 1
    assert shapes[0]["box"] == [716, 21, 1444, 884]  # the Safari window, padded 4 pt
    assert "Here." in says(events)
    assert pool.sessions == {}  # resolved on screen, no brain call
    assert "untrusted" in seen[0]


def test_look_with_nothing_matching_asks_the_brain_with_screen_context(client):
    quiet = FakeConversations(chunks=['{"say": "That is the Safari window."}\n'])
    web = FakeConversations()
    overlay.set_deps(make_deps(conversations=web, quiet_conversations=quiet))
    turn(client, "highlight for me the last debugging session", conv_id="b2", screen=DEBUG_SCREEN)
    assert web.sessions == {}  # screen text only ever goes to the tool-less pool
    prompt = quiet.sessions["b2"].prompts[0]
    assert '<context untrusted="true">' in prompt
    assert "window: Safari: Jarvis voice debug" in prompt
    assert "frontmost app: Terminal" in prompt


def test_follow_up_is_marked_on_the_debug_page(client):
    overlay.set_deps(make_deps(chunks=['{"say": "ok"}\n']))
    turn(client, "and tomorrow?", conv_id="f1", followup=True)
    voice = next(s for t in client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
                 for s in t["steps"] if s["step"] == "voice")
    assert voice["followup"] is True
    assert "follow-up" in overlay.DEBUG_PAGE
