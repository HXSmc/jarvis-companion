"""Hardware Mode part outlines (SAM masks) and plan-length memory on the gateway side.
The real-model test runs only where the SAM files are installed; scenes are synthetic."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from gateway import hwmode
from hwmode.brain import Reply
from hwmode.segment import EDGE, INSIDE, MASK_MAX, Segmenter

MASK = {"target_px": [10, 20, 30, 40], "mask_png": "iVBO", "mask_score": 0.9}


class FakeSegmenter:
    def __init__(self, result=MASK, fail=False):
        self.result, self.fail, self.boxes = result, fail, []

    def embed(self, jpeg):
        return "emb"

    def mask(self, emb, box):
        if self.fail:
            raise RuntimeError("onnx blew up")
        self.boxes.append(box)
        return self.result


def _conn(segmenter):
    conn = hwmode._Conn(ws=None)
    conn.segmenter = segmenter
    return conn


def test_outline_puts_the_mask_on_the_brains_box():
    conn = _conn(FakeSegmenter())
    data = {"step": 1, "say": "There, sir.", "overlay": {"target_px": [5, 15, 35, 45], "label": "SATA cable", "action": "look"}}
    out = asyncio.run(conn._outline(data, b"jpeg", None))
    assert conn.segmenter.boxes == [[5.0, 15.0, 35.0, 45.0]]
    assert out["overlay"] == {"target_px": [10, 20, 30, 40], "label": "SATA cable", "action": "look",
                              "mask_png": "iVBO", "mask_score": 0.9}
    assert out["say"] == "There, sir." and out["step"] == 1


@pytest.mark.parametrize("seg", [FakeSegmenter(result=None), FakeSegmenter(fail=True), None])
def test_no_believable_mask_keeps_the_box(seg):
    conn = _conn(seg)
    data = {"step": 1, "overlay": {"target_px": [5, 15, 35, 45]}}
    assert asyncio.run(conn._outline(data, b"jpeg", None)) == data


def test_no_box_means_no_mask_and_the_early_encode_is_dropped():
    async def run():
        conn = _conn(FakeSegmenter())
        pending = asyncio.create_task(asyncio.sleep(5))
        data = {"step": 1, "overlay": None}
        assert await conn._outline(data, b"jpeg", pending) == data
        await asyncio.sleep(0)
        assert pending.cancelled()
        assert conn.segmenter.boxes == []
    asyncio.run(run())


def test_plan_length_is_remembered_when_a_restarted_brain_forgets_it():
    conn = _conn(None)
    assert conn._plan_length({"step": 1, "of": 4}) == {"step": 1, "of": 4}
    assert conn._plan_length({"step": 2, "of": 0}) == {"step": 2, "of": 4}  # was "2/0" on the phone
    assert conn._plan_length({"step": 0, "of": 0}) == {"step": 0, "of": 0}  # chat before a plan
    assert conn._plan_length(None) is None


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class FreshSession:
    """A brain that was just restarted (turn cap) and answers without the plan length."""

    turns = 0

    def __init__(self):
        self.asked = []

    def ask(self, text, jpeg=None, on_text=None):
        self.asked.append(text)
        return Reply(data={"step": 2, "of": 0, "say": "Still step two, sir.", "overlay": None},
                     raw="", ttft_s=None, total_s=0.1)


def test_restarted_brain_is_told_the_step_and_the_plan_length():
    conn = hwmode._Conn(ws=FakeWS())
    conn.segmenter = None
    conn.session, conn.task, conn.total = FreshSession(), "find the SATA drive", 4
    conn.gate.current = 2
    asyncio.run(conn.ask("what now", use_frame=False))
    assert conn.session.asked[0].startswith("Task: find the SATA drive. We are on step 2 of 4. what now")
    reply = next(m for m in conn.ws.sent if m["type"] == "reply")
    assert (reply["data"]["step"], reply["data"]["of"]) == (2, 4)


def test_plan_prompt_keeps_to_what_the_user_asked():
    from hwmode.brain import SYSTEM_PROMPT

    assert "Plan ONLY what the user asked for" in SYSTEM_PROMPT
    assert "no unplugging, powering down, opening a case" in SYSTEM_PROMPT


# ---------------------------------------------------------------- the real model, if installed

@pytest.fixture(scope="module")
def segmenter():
    seg = Segmenter()
    if not seg.available():
        pytest.skip("SAM 2.1 model files or onnxruntime not installed on this box")
    return seg


def test_real_mask_hugs_each_plug_from_a_loose_box(segmenter):
    from PIL import Image

    from hwmode_synth import iou, render_minipc

    scene = render_minipc(noise=0.1, seed=7)
    emb = segmenter.embed(scene.jpeg)
    assert (emb.width, emb.height) == (1024, 768)
    for part in ("power", "hdmi", "usb"):
        t = scene.boxes[part]
        loose = [t[0] - 15, t[1] - 12, t[2] + 15, t[3] + 12]  # the brain's box is rarely tight
        m = segmenter.mask(emb, loose)
        assert m, f"no mask for {part}"
        print(f"  {part}: loose={loose} mask={m['target_px']} truth={t} iou={iou(m['target_px'], t):.2f}")
        # the power mask also takes in the start of its black cable: same colour, same object
        assert iou(m["target_px"], t) > 0.7 and iou(m["target_px"], t) > iou(loose, t) + 0.25, part
        png = Image.open(io.BytesIO(base64.b64decode(m["mask_png"])))
        assert png.mode == "L" and max(png.size) <= MASK_MAX
        levels = set(png.tobytes())
        assert levels <= {0, INSIDE, EDGE} and {INSIDE, EDGE} <= levels


def test_stray_specks_are_dropped_and_small_holes_filled():
    import numpy as np

    from hwmode.segment import _main_part

    m = np.zeros((100, 100), bool)
    m[30:70, 30:70] = True  # the part
    m[48:52, 48:52] = False  # glare on it
    m[5:9, 85:90] = True  # a speck of table, inside the box
    out = _main_part(m, (0, 0, 99, 99))
    assert out[30:70, 30:70].all()  # one solid piece, hole filled
    assert not out[5:9, 85:90].any()  # the speck is gone
    assert not out[:, 95:].any() and _main_part(np.zeros((10, 10), bool), (0, 0, 9, 9)) is None


def test_real_mask_on_a_dim_table_is_the_part_not_the_table(segmenter):
    """The owner's screenshot: on a dim, grainy wood table SAM liked to paint the table around the
    dock. The mask must be the dock itself, or no mask at all (the phone then shows the box)."""
    import numpy as np
    from PIL import Image

    from hwmode_synth import iou, render_dim_table

    scene = render_dim_table()
    emb = segmenter.embed(scene.jpeg)
    t = scene.boxes["dock"]
    loose = [t[0] - 70, t[1] - 55, t[2] + 70, t[3] + 55]  # a loose box takes in plenty of table
    m = segmenter.mask(emb, loose)
    assert m, "no mask for the dock"
    print(f"  dock: loose={loose} mask={m['target_px']} truth={t} iou={iou(m['target_px'], t):.2f}")
    assert iou(m["target_px"], t) > 0.8
    png = np.asarray(Image.open(io.BytesIO(base64.b64decode(m["mask_png"])))) > 0
    assert png.mean() > 0.75  # the dock is a solid block: its mask fills its own box
