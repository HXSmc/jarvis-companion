"""hide/show/aside/restore/move acks: keyed, short, plain, unique."""

from __future__ import annotations

import pytest

from mac.voice import acks

KINDS = ("hide", "show", "aside", "restore", "move")


@pytest.mark.parametrize("kind", KINDS)
def test_hidemove_ack(kind):
    key = acks.pick_key(kind)
    assert key in acks.PHRASES and key != "generic", kind
    text = acks.PHRASES[key]
    assert len(text.split()) <= 6, text
    assert text.endswith("."), text
    assert list(acks.PHRASES.values()).count(text) == 1, text
    assert "!" not in text, text
    low = text.lower()
    assert not any(w in low for w in ("certainly", "absolutely", "seamless", "i've gone ahead")), text
