"""mac/voice/tts defaults: Jarvis's British voice, not the stock Kokoro one."""

from __future__ import annotations

import importlib


def test_default_voice_bm_daniel(monkeypatch):
    monkeypatch.delenv("KOKORO_VOICE", raising=False)
    import mac.voice.tts as tts

    assert importlib.reload(tts).VOICE == "bm_daniel"
