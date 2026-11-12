"""Overlay jfollow3: the thinking-ack pool and the ack policy's coverage of parse kinds."""

from __future__ import annotations

import random

from mac.voice import acks
from mac.voice.intents import parse


def test_thinking_pool(monkeypatch):
    by_key = {key: (text, tag) for key, text, tag in acks.THINKING_POOL}
    assert len(by_key) >= 12
    for key, (text, tag) in by_key.items():
        assert len(text.split()) <= 8, key
        assert "?" not in text and "!" not in text, key
        assert tag in ("look", "do", "any"), key
        assert acks.PHRASES[key] == text, key
    sir = sum(1 for text, _tag in by_key.values() if ", sir" in text)
    assert 0.55 <= sir / len(by_key) <= 0.8
    # answer turns hear look/any lines, everything else do/any; no key repeats within three.
    cache = acks.AckCache(rng=random.Random(7))
    for kind, tags in (("answer", {"look", "any"}), ("generic", {"do", "any"})):
        recent: list[str] = []
        for _ in range(200):
            key, text, _pcm, _rate = cache.thinking(kind)
            assert by_key[key][1] in tags, (kind, key, text)
            assert acks.PHRASES[key] == text
            assert key not in recent, (key, recent)
            recent.append(key)
            recent = recent[-3:]
    # The sir rate is read at call time: pinned 0 and 1 pin the style.
    monkeypatch.setenv("OVERLAY_SIR_RATE", "0")
    cache = acks.AckCache(rng=random.Random(7))
    for _ in range(50):
        assert ", sir" not in cache.thinking("answer")[1]
    monkeypatch.setenv("OVERLAY_SIR_RATE", "1")
    for _ in range(50):
        assert ", sir" in cache.thinking("answer")[1]
    monkeypatch.delenv("OVERLAY_SIR_RATE", raising=False)
    assert acks.PHRASES["stop"] == "OK."
    # Prerender synthesizes every pool line; a pick then plays the cached clip.
    calls: list[str] = []

    def synth(text):
        calls.append(text)
        return b"pcm:" + text.encode(), 24000

    cache = acks.AckCache(synth=synth, rng=random.Random(3))
    cache.prerender()
    for _key, text, _tag in acks.THINKING_POOL:
        assert text in calls, text
    key, text, pcm, rate = cache.thinking("answer")
    assert (key, rate) in [(k, 24000) for k, _t, _g in acks.THINKING_POOL]
    assert pcm == b"pcm:" + text.encode()


KIND_FIXTURE = [
    ("open safari", {"safari": "/Applications/Safari.app"}, "open_app"),
    ("open example.com", {}, "open_site"),
    ("clear", {}, "clear"),
    ("hide it", {}, "hide"),
    ("hide the cards", {}, "hide"),
    ("show them", {}, "show"),
    ("show the cards", {}, "show"),
    ("park it", {}, "aside"),
    ("park it for now", {}, "aside"),
    ("put them back", {}, "restore"),
    ("move it left", {}, "move"),
    ("move them right", {}, "move"),
    ("what time is it", {}, "clock"),
    ("next change", {}, "hunk"),
    ("previous change", {}, "hunk"),
    ("highlight the changes", {}, "git_diff"),
    ("what changed", {}, "git_diff"),
    ("where is the save button", {}, "ground"),
    ("highlight the search field", {}, "ground"),
    ("highlight the last debugging session", {}, "look"),
    ("highlight the URL", {}, "look"),
    ("check my email", {}, "agent"),
    ("what's on my calendar today", {}, "agent"),
    ("stop", {}, "stop"),
    ("cancel that", {}, "stop"),
    ("enlarge the picture", {}, "card_size"),
    ("make it smaller", {}, "card_size"),
    ("what are my sessions doing", {}, "sessions"),
    ("session status", {}, "sessions"),
    ("tell me a joke", {}, "answer"),
    ("what is the capital of France", {}, "answer"),
]


def test_ack_policy_covers_parse_kinds():
    seen: set[str] = set()
    for text, apps, kind in KIND_FIXTURE:
        intent = parse(text, apps=apps)
        assert intent.kind == kind, (text, intent)
        seen.add(intent.kind)
    # Every kind parse can return is in ACK_POLICY; "media" never comes from parse.
    assert seen == set(acks.ACK_POLICY) - {"media"}, seen
    for kind in ("clock", "media", "stop", "card_size", "open_app", "open_site"):
        assert acks.ack_key(kind) is None, kind
    assert acks.ack_key("agent", "git") == "agent_git"
    assert acks.ack_key("weird") == "generic"
