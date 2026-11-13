"""jfollow4 criterion 53: fixed lines fit the 1.8 s clip limit in Jarvis's own voice.

Runs only where Kokoro and its model files are installed (skipped in CI). The live run measured
16 lines over the limit with this same synth; this box's numbers match the Mac within ~0.07 s,
so lines are held to 1.75 s here.
"""

from __future__ import annotations

import pytest

from hwmode.tts import Voice
from mac.voice import acks
from test_overlay_jfollow4 import NEW_AND_SHORTENED

CLIP_LIMIT_S = 1.8
MARGIN_S = 0.05


def test_new_and_shortened_lines_fit_the_clip_limit():
    voice = Voice()
    if not voice.available():
        pytest.skip("Kokoro (kokoro-onnx + model files) not installed here")
    over = {}
    for key in NEW_AND_SHORTENED:
        pcm, rate = voice.synth(acks.PHRASES[key])
        seconds = len(pcm) / 2 / rate
        if seconds > CLIP_LIMIT_S - MARGIN_S:
            over[key] = round(seconds, 2)
    assert over == {}
