"""hwmode/research: classifiers, web-tools-only lean session, card validation, session text (no network)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from hwmode.research import (
    FALLBACK_SAY,
    ResearchBrain,
    ResearchResult,
    is_research,
    is_start_session,
    session_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hud"

# Stands in for the lean `claude -p --input-format stream-json ...` call: records its argv to
# $FAKE_ARGV and answers every user line with the $FAKE_REPLY text as the model result.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_ARGV"], "w") as f:
    f.write(" ".join(sys.argv[1:]))
reply = os.environ.get("FAKE_REPLY", "")
for line in sys.stdin:
    half = len(reply) // 2
    for part in (reply[:half], reply[half:]):
        print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": part}}}), flush=True)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    argv = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_ARGV", str(argv))
    monkeypatch.setenv("FAKE_REPLY", json.dumps({"say": "It is a desk accessory.", "cards": []}))
    return path, argv


def _ask(fake_claude) -> ResearchResult:
    path, _ = fake_claude
    brain = ResearchBrain(claude_bin=str(path))
    try:
        return brain.ask("what is this")
    finally:
        brain.close()


@pytest.mark.parametrize("text", [
    "find out about the Busy Bar desk display",
    "look it up",
    "look up RTX 5080 price",
    "research the M4 Mac mini",
    "search for Noctua fan specs",
    "what is this",
    "What's this part called",
    "tell me about the LM2596 module",
    "ابحث عن سعر الشاشة",
    "دور لي على كيبل SATA",
    "وش هذا",
    "ما هذا",
    "ايش هذا",
])
def test_is_research_positive(text):
    assert is_research(text)


@pytest.mark.parametrize("text", [
    "done",
    "next",
    "where is the SATA port",
    "is this it?",
    "what is the RAM slot called",
    "ابدأ جلسة عن هذا",
])
def test_is_research_negative(text):
    assert not is_research(text)


@pytest.mark.parametrize("text", [
    "start a session about this",
    "start a session on that",
    "open a session about this",
    "make a session for this",
    "ابدأ جلسة",
    "افتح جلسة",
])
def test_is_start_session_positive(text):
    assert is_start_session(text)


@pytest.mark.parametrize("text", [
    "start the car",
    "open the case",
    "make sure it is plugged in",
    "session over",
])
def test_is_start_session_negative(text):
    assert not is_start_session(text)


def test_research_brain_uses_web_tools_only(fake_claude):
    _, argv = fake_claude
    result = _ask(fake_claude)
    assert result.say == "It is a desk accessory."
    flags = argv.read_text()
    assert "--tools WebSearch,WebFetch" in flags
    assert "--allowedTools WebSearch,WebFetch" in flags
    assert not {"Bash", "Edit", "Write"} & set(flags.split())


def test_research_cards_are_validated(fake_claude, monkeypatch):
    cards = [json.loads((FIXTURES / f"{name}.json").read_text())["cards"][0]
             for name in ("info", "stat", "bar", "flow", "link", "image")]
    cards += [
        cards[0],  # 7th valid card: the cap keeps only 6
        {"id": "c-http", "type": "link", "title": "Insecure", "url": "http://example.com/x"},
        {"id": "c-odd", "type": "arrow", "title": "Unknown kind"},
    ]
    monkeypatch.setenv("FAKE_REPLY", json.dumps({"say": "Here is what I found.", "cards": cards}))
    result = _ask(fake_claude)
    assert [c["id"] for c in result.cards] == ["c-info", "c-stat", "c-bar", "c-flow", "c-link", "c-img"]
    assert all(c["type"] != "arrow" for c in result.cards)


def test_bad_json_gives_a_spoken_fallback(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_REPLY", "sorry, no json in this reply at all")
    result = _ask(fake_claude)
    assert result.say == FALLBACK_SAY
    assert result.cards == []


def test_session_text_has_topic_summary_and_cards():
    cards = [json.loads((FIXTURES / f"{name}.json").read_text())["cards"][0]
             for name in ("info", "stat", "link")]
    result = ResearchResult(say="It is a desk display. It costs about 30 dollars.",
                            cards=cards, raw="", ttft_s=0.5, total_s=2.0)
    text = session_text("Busy Bar desk display", result)
    assert "Topic: Busy Bar desk display" in text
    assert "It is a desk display." in text
    assert '"type":"info"' in text
    assert "Bearer" not in text
    assert len(text) <= 8000


def test_session_text_is_clipped_to_8000_chars():
    result = ResearchResult(say="x" * 20000, cards=[], raw="", ttft_s=None, total_s=1.0)
    assert len(session_text("topic", result)) == 8000
