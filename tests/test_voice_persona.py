"""Voice speed, persona text and ack wording (no model, no network, no claude CLI)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.overlay import ANSWER_PROMPT, PERSONA, QUIET_PROMPT
from hwmode.tts import Voice
from mac.voice import acks, brain


def test_tts_speed_default_and_override(monkeypatch):
    monkeypatch.delenv("HWMODE_TTS_SPEED", raising=False)
    assert Voice().speed == 1.08
    monkeypatch.setenv("HWMODE_TTS_SPEED", "0.9")
    assert Voice().speed == 0.9


def test_persona_size_and_rules():
    assert len(PERSONA.split()) <= 120
    low = PERSONA.lower()
    assert "no humour for" in low
    assert "never claim what you did not do" in low
    assert "cards stay factual" in low
    for banned in ("stark", "iron man", "marvel", "tony", "butler"):
        assert banned not in low


def test_prompts_carry_persona():
    assert ANSWER_PROMPT.startswith(PERSONA)
    assert QUIET_PROMPT.startswith(PERSONA)
    for kept in ("clock", "incomplete", "JSON Lines", "untrusted"):
        assert kept in QUIET_PROMPT


def test_fallback_brain_prompt_keeps_limits():
    assert "no tools" in brain._SYSTEM_PROMPT
    assert "at most two short spoken sentences" in brain._SYSTEM_PROMPT


def test_ack_phrases_speakable():
    assert set(acks.PHRASES) == {"open", "look", "diff", "answer", "clear", "generic"}
    for text in acks.PHRASES.values():
        assert text and len(text.split()) <= 6
        assert "?" not in text and "!" not in text
