"""jfollow4 helper A: the new and shortened ack lines exist, stay short and prerender."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac.voice import acks

NEW_AND_SHORTENED = {
    "busy": "Still on the last one.",
    "no_sessions_access": "I can't reach your sessions.",
    "diff_none_auto": "No uncommitted changes here.",
    "session_none_named": "I don't see a session called that, sir.",
    "session_device": "Sessions start on the mini PC only, sir.",
    "session_failed": "That didn't go through, sir.",
    "session_limit": "That's the limit for this hour, sir.",
    "session_ask_gone": "That question is no longer open, sir.",
    "session_started_it": "Started it on the mini PC, sir.",
    "session_sent": "Sent.",
    "memory_none": "I don't have anything about that, sir.",
    "memory_empty": "I don't remember anything yet, sir.",
    "memory_forgot": "Forgotten, sir.",
    "memory_forgot_all": "All forgotten, sir.",
    "memory_failed": "I couldn't save that, sir.",
    "workspace_failed": "I couldn't save that one, sir.",
    "workspace_none": "I have nothing saved by that name.",
    "workspace_empty": "There's nothing on screen to save.",
    "workspace_full": "That's thirty saved. Delete one first.",
    "workspace_deleted": "Deleted, sir.",
    "workspace_none_saved": "You haven't saved anything yet, sir.",
    "workspace_no_pages": "No pages were saved there.",
    "vault_off_limits": "That folder is off limits, sir.",
    "cut_off": "I didn't catch the end of that, sir.",
}


def test_new_and_shortened_lines():
    rendered: list[str] = []

    def fake_synth(text):
        rendered.append(text)
        return b"pcm", 24000

    cache = acks.AckCache(synth=fake_synth)
    cache.prerender()
    for key, text in NEW_AND_SHORTENED.items():
        assert acks.PHRASES.get(key) == text, key
        assert len(text.split()) <= 8, key
        assert "?" not in text and "!" not in text, key
        assert text in rendered, key
        assert cache.get(key) == (text, b"pcm", 24000), key
