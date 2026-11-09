"""Live VLM grounding over the fixture screens — opt-in via OVERLAY_LIVE=1.

Each turn is a real sonnet call, so a normal `pytest tests/` never runs this
(skipif below). One persistent lean session serves all scenes, text only: the
fixtures carry no screenshot, the candidate list is the whole input.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from mac.voice.ground import build_candidates, pick

pytestmark = pytest.mark.skipif(
    os.environ.get("OVERLAY_LIVE") != "1", reason="set OVERLAY_LIVE=1 to spend real sonnet calls"
)

SCREENS = Path(__file__).parent / "fixtures" / "overlay" / "screens"

GROUNDING_SYSTEM = (
    "You ground UI elements on a macOS screen for an overlay assistant. "
    'Follow the user message exactly: reply with only {"id": <n>} or {"id": null}, no prose.'
)

try:
    from mac.voice.lean import LeanSession
except ImportError:
    from hwmode.brain import LeanSession  # same lean flags and ctor, no lean.py yet


def test_fixture_grounding_live():
    session = LeanSession(system_prompt=GROUNDING_SYSTEM)
    try:
        hits, times = 0, []
        for f in sorted(SCREENS.glob("*.json")):
            scene = json.loads(f.read_text())
            cands = build_candidates(scene["ax"], scene["ocr"], scale=scene["scale"])
            gt = [c for c in cands if c["label"] == scene["gt_label"]][0]
            start = time.monotonic()
            got = pick(scene["question"], cands, None, lambda p, j: session.ask(p, j).raw)
            times.append((time.monotonic() - start) * 1000)
            if got is not None and got["box"] == gt["box"]:
                hits += 1
        total = len(times)
        print(f"hit_rate={hits}/{total} p50_ms={sorted(times)[total // 2]:.0f}")
        assert hits / total >= 0.8
    finally:
        session.close()
