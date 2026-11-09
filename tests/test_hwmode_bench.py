"""Box accuracy + first-speech bench over synthetic scenes, through the real gateway pipeline
(real brain, zoom refine, SAM tightening when installed, Kokoro voice when installed).

Opt-in (real model calls):
  HWMODE_LIVE=1 HWMODE_BENCH_OUT=out.json python -m pytest tests/test_hwmode_bench.py -q -s
Real SAM and real Kokoro voice are on (markers live_mask, live_voice). Writes per-case IoU and hit (box centre inside the ground-truth box) for each run, plus the
time from `ask` to the first `say` and first `audio` frame. It only speaks the wire protocol
(start / frame / ask, box from `shape.box` or `overlay.target_px`), so the same file runs on
older builds for a before/after comparison.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import websockets

from conftest import IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from hwmode_synth import MINIPC_TASK, TASK, center_in, iou, render, render_minipc

pytestmark = [pytest.mark.skipif(os.environ.get("HWMODE_LIVE") != "1", reason="live model calls: set HWMODE_LIVE=1"),
              pytest.mark.live_mask, pytest.mark.live_voice]

# (case id, task, scene factory, question, ground-truth part)
CASES = [
    ("sata2", TASK, lambda: render("in_port1"), "Where is the SATA2 port? Point at it.", "SATA2"),
    ("plug_seated", TASK, lambda: render("in_port1"), "Where is the SATA cable plug right now? Point at it.", "connector"),
    ("plug_loose", TASK, lambda: render("unplugged"), "Where is the loose SATA cable plug? Point at it.", "connector"),
    ("sata2_dim", TASK, lambda: render("unplugged", noise=0.5, brightness=0.45, seed=3),
     "Where is the SATA2 port? Point at it.", "SATA2"),
    ("sata3_shake", TASK, lambda: render("in_port1", shift=(40, -30), blur=1.5),
     "Where is the SATA3 port? Point at it.", "SATA3"),
    ("sata2_ar", TASK, lambda: render("unplugged"), "وين منفذ SATA2؟ أشر عليه.", "SATA2"),
    ("power_plug", MINIPC_TASK, render_minipc, "Point at the power plug.", "power"),
    ("white_cable", MINIPC_TASK, render_minipc, "Where is the white cable?", "usb"),
    ("hdmi", MINIPC_TASK, render_minipc, "Where is the HDMI cable?", "hdmi"),
    ("screw_tl", MINIPC_TASK, render_minipc, "Point at the top left screw.", "screw_tl"),
    ("screw_br", MINIPC_TASK, render_minipc, "Point at the bottom right screw.", "screw_br"),
]
RUNS = int(os.environ.get("HWMODE_BENCH_RUNS", "2"))
_rows: list[dict] = []


def _box(data: dict | None):
    d = data or {}
    shape = d.get("shape") if isinstance(d.get("shape"), dict) else None
    o = d.get("overlay") if isinstance(d.get("overlay"), dict) else None
    px = (shape or {}).get("box") or (o or {}).get("target_px")
    return [float(v) for v in px] if isinstance(px, list) and len(px) == 4 else None


async def _one(base: str, task: str, jpeg: bytes, question: str) -> dict:
    async with websockets.connect(f"ws://{base}/hwmode/ws", max_size=None,
                                  **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
        await ws.send(json.dumps({"type": "start", "task": task}))
        while (ready := json.loads(await asyncio.wait_for(ws.recv(), 60)))["type"] != "ready":
            pass
        voiced = bool(ready.get("voice"))
        await ws.send(json.dumps({"type": "frame", "id": 1, "jpeg": base64.b64encode(jpeg).decode()}))
        t0 = time.monotonic()
        await ws.send(json.dumps({"type": "ask", "text": question, "frame": True}))
        first_say = first_audio = result = None
        while True:
            try:  # after the reply, wait a little for its audio (Arabic lines have none)
                msg = json.loads(await asyncio.wait_for(ws.recv(), 20 if result else 120))
            except asyncio.TimeoutError:
                if result is None:
                    raise
                return {**result, "first_say_s": first_say, "first_audio_s": first_audio}
            now = round(time.monotonic() - t0, 3)
            if msg["type"] == "say" and first_say is None:
                first_say = now
            elif msg["type"] in ("audio", "ack") and first_audio is None:  # an instant ack is audio too
                first_audio = now
            elif msg["type"] == "reply" and result is None:
                result = {"box": _box(msg.get("data")), "reply_s": now}
            if result is not None and (first_audio is not None or not voiced):
                return {**result, "first_say_s": first_say, "first_audio_s": first_audio}
            elif msg["type"] == "error" and msg.get("error") != "busy":
                return {"box": None, "error": msg.get("error"), "reply_s": now,
                        "first_say_s": first_say, "first_audio_s": first_audio}


@pytest.mark.parametrize("run", range(RUNS))
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_bench_case(gateway_server, case, run):
    cid, task, scene_of, question, part = case
    scene = scene_of()
    truth = scene.boxes[part]

    async def go():
        async with gateway_server() as base:
            return await _one(base, task, scene.jpeg, question)

    r = asyncio.run(go())
    box = r["box"]
    row = {"case": cid, "run": run, "truth": list(truth), **r,
           "iou": round(iou(box, truth), 3) if box else 0.0, "hit": bool(box and center_in(box, truth, margin=0))}
    _rows.append(row)
    print(json.dumps(row))


def test_bench_write_summary():
    """Runs last (file order): per-case means + overall mean IoU / hit rate / first-audio p50."""
    out = os.environ.get("HWMODE_BENCH_OUT")
    if not _rows:
        pytest.skip("no bench rows")
    cases = {}
    for r in _rows:
        cases.setdefault(r["case"], []).append(r)
    summary = {cid: {"iou": round(statistics.mean(x["iou"] for x in rs), 3),
                     "hit_rate": round(sum(x["hit"] for x in rs) / len(rs), 3), "runs": len(rs)}
               for cid, rs in cases.items()}
    audio = [r["first_audio_s"] for r in _rows if r.get("first_audio_s") is not None]
    say = [r["first_say_s"] for r in _rows if r.get("first_say_s") is not None]
    result = {"mean_iou": round(statistics.mean(r["iou"] for r in _rows), 3),
              "hit_rate": round(sum(r["hit"] for r in _rows) / len(_rows), 3),
              "first_audio_p50_s": round(statistics.median(audio), 3) if audio else None,
              "first_say_p50_s": round(statistics.median(say), 3) if say else None,
              "cases": summary, "rows": _rows}
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1))
    if out:
        Path(out).write_text(json.dumps(result, indent=1))
