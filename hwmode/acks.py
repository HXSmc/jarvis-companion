"""Instant spoken acknowledgements for Hardware Mode ("Checking.", "Right.").

A fixed set of short lines, rendered once with Jarvis's own Kokoro voice in a background
thread and kept in memory, so the gateway can answer the end of the owner's speech in well
under a second while the brain (or a web research turn) is still working.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# kind -> line. Kept tiny on purpose: every line is rendered up front.
LINES = {
    "research": "Checking.",
    "done": "Right.",
    "wait": "One moment.",
    "session": "Starting that session now.",
}


class Acks:
    def __init__(self, voice) -> None:
        self.voice = voice
        self.clips: dict[str, tuple[bytes, int]] = {}
        self.ready = threading.Event()
        threading.Thread(target=self._render, name="hwmode-acks", daemon=True).start()

    def _render(self) -> None:
        for line in LINES.values():
            try:
                self.clips[line] = self.voice.synth(line)
            except Exception:  # no clip just means no instant ack for that line
                logger.exception("hwmode: ack clip failed: %r", line)
        self.ready.set()

    def clip(self, kind: str) -> tuple[str, bytes, int] | None:
        """(line, pcm, rate) for `kind` (unknown kinds get the neutral "wait" line), or None
        while the clips are still rendering."""
        line = LINES.get(kind, LINES["wait"])
        found = self.clips.get(line)
        return (line, *found) if found else None


_acks: Acks | None = None
_lock = threading.Lock()


def get_acks(voice) -> Acks | None:
    """The process-wide ack clips for `voice` (rendering starts on the first call), or None
    when there is no server voice (the phone speaks for itself then)."""
    global _acks
    if voice is None:
        return None
    with _lock:
        if _acks is None or _acks.voice is not voice:
            _acks = Acks(voice)
        return _acks
