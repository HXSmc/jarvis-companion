"""f52-lookfix: own-card line only when nothing on screen matches, a conversation life cycle so
pictures work again after a screen question, and small look-path fixes. Fakes only: no model,
no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import overlay  # noqa: E402

# --- D7: ids and labels the look fallback points at ---------------------------------------------

TARGETS = [{"id": 1, "source": "ocr", "role": "text", "label": "Stats", "box": [0, 0, 10, 10]},
           {"id": 2, "source": "ocr", "role": "text", "label": 'Total  "tokens" <b>', "box": [0, 20, 10, 30]}]


def test_point_digits():
    assert overlay._point_at("²", TARGETS) is None  # "²".isdigit() is True; int("²") used to raise
    assert overlay._point_at("1" * 30, TARGETS) is None
    assert overlay._point_at("٣", TARGETS) is None  # non-ASCII digits are text, and too short
    hit, via = overlay._point_at(2.0, TARGETS)
    assert (hit["id"], via) == (2, "point_id")
    assert overlay._point_at(2.5, TARGETS) is None and overlay._point_at(True, TARGETS) is None


def test_point_safe_label():
    hit, via = overlay._point_at("total tokens b", TARGETS)  # the label as the model saw it
    assert (hit["id"], via) == (2, "point_text")
