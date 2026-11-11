"""Look turns that follow a question ("what's my total tokens?" -> "can you highlight it for me").

Grounding gets the last turns, so "it" resolves to the line the conversation was about; no
screen gives one plain line; every look/ground turn leaves one `ground` debug step holding
counts and ids only. Fakes only: no model, no network.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from conftest import AUTH_HEADER  # noqa: E402
from gateway import overlay  # noqa: E402
from test_overlay_service import (  # noqa: E402,F401  (shared stubs + fixtures)
    FakeConversations, client, make_deps, reset_overlay_state, says, turn,
)

# Labels hold no "@" and no four comma-separated numbers (the no-coordinates check relies on it).
OCR_TEXT = ["Stats", "Overview", "Models", "Favorite model: Sonnet", "Sessions: 412",
            "Total tokens: 32.0b", "Longest streak: 9 days", "Active days: 120", "Peak hour: 10 am",
            "Export data"]
TARGET = "Total tokens: 32.0b"
ASK_1 = "what's my total tokens?"
SAY_1 = "Total tokens is 32.0b."
FOLLOWUP = "Can you highlight it for me"


def screen(texts=OCR_TEXT) -> dict:
    """OCR lines in capture px at scale 2: line i sits at [50, 50+30i, 350, 70+30i] in points."""
    return {"scale": 2.0, "app": "Claude",
            "ocr": [{"text": t, "box": [100, 100 + 60 * i, 700, 140 + 60 * i]} for i, t in enumerate(texts)]}


def box_pt(texts, label) -> list[float]:
    i = texts.index(label)
    return [50.0, 50.0 + 30 * i, 350.0, 70.0 + 30 * i]


def history_of(prompt: str) -> str:
    m = re.search(r'<history untrusted="true">\n(.*?)\n</history>', prompt, re.S)
    return m.group(1) if m else ""


def id_of(prompt: str, label: str) -> str:
    return re.search(r'\[(\d+)\] [^\n]*"' + re.escape(label) + '"', prompt).group(1)


class HistoryVLM:
    """Picks `label` only when `needle` is in the prompt's history block; else `fallback` (or null)."""

    def __init__(self, label=TARGET, needle="Total tokens", fallback=None, delay=0.0):
        self.label, self.needle, self.fallback, self.delay = label, needle, fallback, delay
        self.prompts: list[str] = []

    def __call__(self, prompt, jpeg):
        self.prompts.append(prompt)
        if self.delay:
            time.sleep(self.delay)
        if self.needle in history_of(prompt):
            return '{"id": %s}' % id_of(prompt, self.label)
        return '{"id": %s}' % (id_of(prompt, self.fallback) if self.fallback else "null")


def answer_prompts(deps) -> list[str]:
    return [p for pool in (deps.conversations, deps.quiet_conversations)
            for s in pool.sessions.values() for p in s.prompts]


def ground_steps(client) -> list[dict]:
    newest = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"][0]
    return [s for s in newest["steps"] if s["step"] == "ground"]


def fields(detail: str) -> dict:
    return dict(re.findall(r'(\w+)=("[^"]*"|\S+)', detail))


def shapes(events) -> list[dict]:
    return [e for e in events if e["type"] == "shape"]


def near(box, want, tol=4.0) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(box, want, strict=True))


def followup(client, vlm=None, chunks=(f'{{"say": "{SAY_1}"}}\n',), conv="f1"):
    vlm = vlm or HistoryVLM()
    deps = make_deps(vlm=vlm, chunks=chunks)
    overlay.set_deps(deps)
    turn(client, ASK_1, conv_id=conv, screen=screen())
    before = len(answer_prompts(deps))
    events = turn(client, FOLLOWUP, conv_id=conv, followup=True, screen=screen())
    return events, deps, vlm, len(answer_prompts(deps)) - before


# --- history resolves "it" ----------------------------------------------------


def test_followup_highlight_it_draws_the_line_from_history(client):
    events, _deps, vlm, answer_calls = followup(client)
    got = shapes(events)
    assert len(got) == 1
    assert near(got[0]["shape"]["box"], box_pt(OCR_TEXT, TARGET))
    assert got[0]["shape"]["label"] == TARGET
    assert says(events)[-1] == "Here."
    assert answer_calls == 0  # a hit never reaches the answer model
    assert "Total tokens is 32.0b" in history_of(vlm.prompts[-1])


def test_followup_history_picks_between_two_candidates(client):
    """Live repro: asked about the motto, then "highlight it" boxed the Flag. History decides."""
    texts = ["Saudi Arabia", "Flag", "Emblem", "Motto: There is no god but God", "Capital: Riyadh"]
    vlm = HistoryVLM(label="Motto: There is no god but God", needle="motto", fallback="Flag")
    overlay.set_deps(make_deps(vlm=vlm, chunks=('{"say": "The motto on the page is the Shahada."}\n',)))
    turn(client, "what does the motto on this page say in English", conv_id="m1", screen=screen(texts))
    events = turn(client, "highlight it for me", conv_id="m1", followup=True, screen=screen(texts))
    got = shapes(events)
    assert len(got) == 1 and got[0]["shape"]["label"] == "Motto: There is no god but God"
    assert near(got[0]["shape"]["box"], box_pt(texts, "Motto: There is no god but God"))


def test_named_first_turn_target_still_works(client):
    vlm = HistoryVLM(label="Stats", needle="")  # "" is in any history, also an empty one
    overlay.set_deps(make_deps(vlm=vlm))
    events = turn(client, "highlight the title", conv_id="n1", screen=screen())
    assert len(shapes(events)) == 1 and shapes(events)[0]["shape"]["label"] == "Stats"
    assert says(events)[-1] == "Here."
    assert "<history" not in vlm.prompts[0]  # first turn: the prompt is today's


# --- no screen ----------------------------------------------------------------


def test_no_screen_look_turn_says_one_line(client):
    calls = []
    deps = make_deps(vlm=lambda p, j: calls.append(p) or '{"id": 1}')
    overlay.set_deps(deps)
    events = turn(client, "highlight it for me", conv_id="ns1", audio_ms=900)  # heard by the app
    assert says(events)[1:] == [overlay.NO_SCREEN_LINE]  # the look ack comes first
    assert shapes(events) == [] and calls == [] and answer_prompts(deps) == []
    assert "screen recording" in overlay.NO_SCREEN_LINE.lower()


def test_no_screen_typed_look_turn_still_asks_the_brain(client):
    """A typed POST (no audio_ms) never carries a capture: the brain answers as before."""
    deps = make_deps(chunks=('{"say": "The last session is the top one."}\n',))
    overlay.set_deps(deps)
    events = turn(client, "highlight it for me", conv_id="ns2")
    assert overlay.NO_SCREEN_LINE not in says(events) and len(answer_prompts(deps)) == 1
    assert len(ground_steps(client)) == 1 and fields(ground_steps(client)[0]["detail"])["screen"] == "none"


# --- debug step + log hygiene -------------------------------------------------


def _check_detail(step: dict, picked: str | None) -> dict:
    detail = step["detail"]
    got = fields(detail)
    for key in ("screen", "image", "candidates", "history", "pick", "reason", "via", "ms"):
        assert key in got, key
    for text in OCR_TEXT:
        if text != picked:
            assert text not in detail, text
    for text in (ASK_1, SAY_1, "32.0b."):
        assert text not in detail
    if picked:
        assert len(got["label"].strip('"')) <= 40
    else:
        assert "label" not in got
    return got


def test_debug_step_followup_hit(client):
    followup(client)
    steps = ground_steps(client)
    assert len(steps) == 1
    got = _check_detail(steps[0], TARGET)
    assert (got["screen"], got["image"], got["history"]) == ("yes", "no", "1")
    assert (got["reason"], got["via"]) == ("picked", "ground")
    assert got["pick"] != "null" and int(got["candidates"]) == len(OCR_TEXT)


def test_debug_step_no_screen(client):
    overlay.set_deps(make_deps())
    turn(client, "highlight it for me", conv_id="dn1", audio_ms=900)
    steps = ground_steps(client)
    assert len(steps) == 1
    got = _check_detail(steps[0], None)
    assert (got["screen"], got["reason"], got["via"], got["pick"]) == ("none", "no_screen", "none", "null")


def test_debug_step_vlm_timeout_falls_back_to_the_answer(client, monkeypatch):
    monkeypatch.setattr(overlay, "GROUND_TIMEOUT_S", 0.05)
    events, _deps, _vlm, answer_calls = followup(
        client, vlm=HistoryVLM(delay=0.3), chunks=('{"say": "It is the Total tokens line."}\n',))
    assert answer_calls == 1 and "It is the Total tokens line." in says(events)
    steps = ground_steps(client)
    assert len(steps) == 1
    got = _check_detail(steps[0], None)
    assert (got["reason"], got["pick"]) == ("timeout", "null")


def test_debug_step_on_a_ground_turn(client):
    overlay.set_deps(make_deps(vlm=lambda p, j: '{"id": null}'))
    events = turn(client, "where is the Export button", conv_id="dg1", screen=screen())
    assert "I can't see it on screen." in says(events)
    steps = ground_steps(client)
    assert len(steps) == 1 and fields(steps[0]["detail"])["reason"] == "null"


def test_log_has_no_screen_or_history_text(client, caplog):
    caplog.set_level(logging.DEBUG)
    followup(client)  # a hit
    followup(client, vlm=HistoryVLM(needle="never there"), conv="f2")  # a miss -> the answer fallback
    for rec in caplog.records:
        msg = rec.getMessage()
        for text in OCR_TEXT:
            if text != TARGET:
                assert text not in msg, (text, msg)
        assert ASK_1 not in msg and SAY_1 not in msg
    ground_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("overlay ground ")]
    assert len(ground_lines) == 2


# --- prompt cleanup ------------------------------------------------------------


def test_prompt_never_claims_text_only():
    bad = re.compile(r"(?i)only (get|have|see) .*text|no positions|not where it sits")
    for prompt in (overlay.ANSWER_PROMPT, overlay.QUIET_PROMPT):
        for sentence in re.split(r"(?<=[.!?])\s+", prompt):
            assert not bad.search(sentence), sentence
        assert "never say you cannot see where things are" in prompt
    for banned in ("WebSearch", "WebFetch", "image_search", "video_search"):
        assert banned not in overlay.QUIET_PROMPT
