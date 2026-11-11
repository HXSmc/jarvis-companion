"""f52-movefix: spoken overlay lines stay short, plain and never re-trigger a layout intent."""

import pytest

from gateway import overlay
from mac.voice import acks
from mac.voice import intents

# restore ("Putting them back.") is checked for parse only: "back" is a gate word by design
# (the owner says "put them back"), and the line still parses as a plain answer.
_LINES = [overlay.OVERLAY_UNCHANGED_LINE] + [acks.PHRASES[k] for k in ("hide", "show", "aside", "restore", "move", "clear")]


@pytest.mark.parametrize("line", _LINES)
def test_wording_lines_are_short_plain_and_inert(line):
    assert len(line.split()) <= 6 and line.endswith(".") and "!" not in line
    assert intents.parse(line, apps={}).kind == "answer"


@pytest.mark.parametrize("line", [l for l in _LINES if l != acks.PHRASES["restore"]])
def test_wording_lines_hold_no_gate_word(line):
    assert overlay._OVERLAY_VERB_RE.search(line) is None


def test_wording_exact_values():
    assert acks.PHRASES["show"] == "Showing them."
    assert acks.PHRASES["move"] == "Moving them."
    assert overlay.OVERLAY_UNCHANGED_LINE == "The overlay is unchanged."
