"""Spoken acknowledgements: one short line per intent, pre-rendered to PCM.

The ack is the first event of every turn, so the phrases are synthesized once
up front and cached; a missing or failing synthesizer degrades to the text
form and never blocks the turn.
"""

from __future__ import annotations

from collections.abc import Callable

PHRASES = {
    "open": "On it.",
    "look": "Let me look.",
    "diff": "Pulling up the changes now.",
    "answer": "One moment.",
    "clear": "Cleared.",
    "generic": "Right away.",
    "agent_git": "Checking the git history.",
    "agent_gh": "Looking at GitHub.",
    "agent_notes": "Searching your notes.",
    "agent_mail": "Checking your mail.",
    "agent_calendar": "Checking your calendar.",
    "agent_code": "Looking at that file.",
    "progress_git": "Reading the git log.",
    "progress_gh": "Checking GitHub.",
    "progress_notes": "Reading your notes.",
    "progress_file": "Reading the file.",
    "progress_mail": "Reading your mail.",
    "progress_calendar": "Reading your calendar.",
    "progress_other": "Still working on it.",
    "stopped": "Stopped.",
    "timeout": "That took too long, so I stopped.",
    "busy": "Still working on the last one.",
    "not_confirmed": "Alright, I won't.",
    "expired": "That request expired, so I didn't do it.",
}

_ACK_KEYS = {
    "open_app": "open",
    "open_site": "open",
    "ground": "look",
    "look": "look",
    "git_diff": "diff",
    "clear": "clear",
    "answer": "answer",
}


def pick_key(intent_kind: str, sub: str = "") -> str:
    """Intent kind -> PHRASES key; unknown kinds get the generic ack."""
    if intent_kind == "agent":
        return f"agent_{sub}" if f"agent_{sub}" in PHRASES else "agent_code"
    return _ACK_KEYS.get(intent_kind, "generic")


def progress_key(kind: str) -> str:
    """Agent sub -> progress-line key; unknown subs get the generic progress line."""
    return f"progress_{kind}" if f"progress_{kind}" in PHRASES else "progress_other"


class AckCache:
    """Prerendered ack clips; get() always returns a speakable (text, pcm, rate)."""

    def __init__(self, synth: Callable[[str], tuple[bytes, int]] | None = None):
        # synth(text) -> (pcm bytes, rate); None = text-only acks.
        self._synth = synth
        self._clips: dict[str, tuple[bytes, int]] = {}

    def prerender(self) -> None:
        """Render every phrase once; a phrase whose synth raises stays text-only."""
        if self._synth is None:
            return
        for key, text in PHRASES.items():
            try:
                self._clips[key] = self._synth(text)
            except Exception:
                continue

    def get(self, key: str) -> tuple[str, bytes | None, int]:
        if key not in PHRASES:
            key = "generic"
        pcm, rate = self._clips.get(key, (None, 0))
        return PHRASES[key], pcm, rate
