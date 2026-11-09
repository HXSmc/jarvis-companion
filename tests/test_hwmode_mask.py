"""Hardware Mode part outlines (SAM masks) on the gateway side, and what a restarted
brain is told. The real-model test runs only where the SAM files are installed."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from gateway import hwmode
from hwmode.brain import Reply
from hwmode.segment import Segmenter

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
    data = {"next_ready": False, "done_seen": False, "say": "There, sir.",
            "shape": {"kind": "rect", "box": [5, 15, 35, 45], "label": "SATA cable"}}
    out = asyncio.run(conn._outline(data, b"jpeg", None))
    assert conn.segmenter.boxes == [[5.0, 15.0, 35.0, 45.0]]
    assert out["shape"] == {"kind": "rect", "box": [10.0, 20.0, 30.0, 40.0], "label": "SATA cable"}
    assert out["say"] == "There, sir." and out["next_ready"] is False


def test_a_mask_that_only_trims_the_rim_keeps_the_brains_box():
    seg = FakeSegmenter()
    seg.result = {"target_px": [302, 422, 419, 468], "mask_score": 0.9}  # 1-3 px inside a 120x50 box
    conn = _conn(seg)
    data = {"say": "There, sir.", "shape": {"kind": "rect", "box": [300, 420, 420, 470]}}
    assert asyncio.run(conn._outline(data, b"jpeg", None))["shape"]["box"] == [300, 420, 420, 470]
    seg.result = {"target_px": [346, 514, 453, 553], "mask_score": 0.9}  # blurred edges: up to 15% inside
    data = {"say": "There, sir.", "shape": {"kind": "rect", "box": [342, 512, 460, 560]}}
    assert asyncio.run(conn._outline(data, b"jpeg", None))["shape"]["box"] == [342, 512, 460, 560]
    seg.result = {"target_px": [373, 257, 673, 479], "mask_score": 0.9}  # a loose box really corrected
    data = {"say": "There, sir.", "shape": {"kind": "rect", "box": [310, 205, 730, 525]}}
    assert asyncio.run(conn._outline(data, b"jpeg", None))["shape"]["box"] == [373.0, 257.0, 673.0, 479.0]


@pytest.mark.parametrize("seg", [FakeSegmenter(result=None), FakeSegmenter(fail=True), None])
def test_no_believable_mask_keeps_the_box(seg):
    conn = _conn(seg)
    data = {"say": "There, sir.", "shape": {"kind": "rect", "box": [5, 15, 35, 45]}}
    assert asyncio.run(conn._outline(data, b"jpeg", None)) == data


def test_no_box_means_no_mask_and_the_early_encode_is_dropped():
    async def run():
        conn = _conn(FakeSegmenter())
        pending = asyncio.create_task(asyncio.sleep(5))
        data = {"say": "There.", "shape": None}
        assert await conn._outline(data, b"jpeg", pending) == data
        await asyncio.sleep(0)
        assert pending.cancelled()
        assert conn.segmenter.boxes == []
    asyncio.run(run())


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class FreshSession:
    """A brain that was just restarted (turn cap) and answers without any memory."""

    turns = 0

    def __init__(self):
        self.asked = []

    def ask(self, text, jpeg=None, on_text=None):
        self.asked.append(text)
        return Reply(data={"next_ready": False, "done_seen": False, "say": "Still here, sir.", "shape": None},
                     raw="", ttft_s=None, total_s=0.1)


def test_restarted_brain_is_told_the_task_and_the_current_instruction():
    conn = hwmode._Conn(ws=FakeWS())
    conn.segmenter = None
    conn.session, conn.task, conn.last_say = FreshSession(), "find the SATA drive", "Point at the SSD, sir."
    asyncio.run(conn.ask("what now", use_frame=False))
    assert conn.session.asked[0].startswith(
        "Task: find the SATA drive. The current instruction was: «Point at the SSD, sir.». what now")
    reply = next(m for m in conn.ws.sent if m["type"] == "reply")
    assert not {"step", "of"} & reply["data"].keys()
    assert reply["data"]["say"] == "Still here, sir."


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
