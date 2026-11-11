"""Spoken acknowledgements: one short line per intent, pre-rendered to PCM.

The ack is the first event of every turn, so the phrases are synthesized once
up front and cached; a missing or failing synthesizer degrades to the text
form and never blocks the turn.
"""

from __future__ import annotations

from collections.abc import Callable

PHRASES = {
    "open": "Of course.",
    "look": "Having a look.",
    "diff": "Pulling up the changes.",
    "answer": "Just a moment.",
    "clear": "All cleared.",
    "generic": "On it.",
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


def pick_key(intent_kind: str) -> str:
    """Intent kind -> PHRASES key; unknown kinds get the generic ack."""
    return _ACK_KEYS.get(intent_kind, "generic")


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
