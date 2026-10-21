"""Jarvis's own voice for Hardware Mode: Kokoro TTS on this box (CPU), the same British
voice the jarvis-phone assistant uses (`bm_daniel`). A stock voice, no cloning.

Optional by design: without the `kokoro-onnx` package or its model files the gateway
says so in "ready" and the phone speaks with its built-in voice instead.
Model files (kokoro-v1.0.int8.onnx, voices-v1.0.bin) come from the kokoro-onnx GitHub
releases; they live in HWMODE_TTS_DIR (default ~/.local/share/kokoro), not in the repo.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

_ARABIC = re.compile(r"[؀-ۿ]")


class Voice:
    def __init__(self) -> None:
        self.name = os.environ.get("HWMODE_TTS_VOICE", "bm_daniel")
        self.speed = float(os.environ.get("HWMODE_TTS_SPEED", "1.0"))
        self.dir = Path(os.environ.get("HWMODE_TTS_DIR", str(Path.home() / ".local/share/kokoro")))
        self._kokoro = None
        self._lock = threading.Lock()  # one synthesis at a time: onnxruntime already uses every core

    @property
    def model(self) -> Path:
        return self.dir / "kokoro-v1.0.int8.onnx"

    @property
    def voices(self) -> Path:
        return self.dir / "voices-v1.0.bin"

    def available(self) -> bool:
        if not (self.model.is_file() and self.voices.is_file()):
            return False
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def speaks(text: str) -> bool:
        """English only: Arabic lines are left to the phone's own Arabic voice."""
        return bool(text.strip()) and not _ARABIC.search(text)

    def synth(self, text: str) -> tuple[bytes, int]:
        """(16-bit mono PCM, sample rate) for `text`. Blocking (~0.3x real time on this box)."""
        import numpy as np

        with self._lock:
            if self._kokoro is None:
                from kokoro_onnx import Kokoro

                self._kokoro = Kokoro(str(self.model), str(self.voices))
            samples, rate = self._kokoro.create(text, voice=self.name, speed=self.speed, lang="en-gb")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        return pcm, int(rate)


_voice: Voice | None = None


def get_voice() -> Voice | None:
    """The process-wide voice, or None when it is not installed (the phone speaks instead)."""
    global _voice
    if _voice is None:
        _voice = Voice()
    return _voice if _voice.available() else None
