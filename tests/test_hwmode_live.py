"""Hardware Mode end to end with the REAL brain (the `claude` CLI on this box) behind the
real gateway app, fed synthetic frames from tests/hwmode_synth.py. Opt-in, because every
test makes real model calls:  HWMODE_LIVE=1 python -m pytest tests/test_hwmode_live.py -v

Suites: vision (the pointer lands on the asked part), step gating (no advance until the
step is done; wrong action gets a correction), full-flow replay (event log order).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import websockets

from conftest import IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from hwmode_synth import STEPS_HINT, TASK, center_in, iou, render, video

pytestmark = pytest.mark.skipif(os.environ.get("HWMODE_LIVE") != "1", reason="live model calls: set HWMODE_LIVE=1")

# The phone's prompts, word for word (HardwareModeView.swift).
PLAN = "Plan the steps, then guide me through step 1."
WATCH = ('[watch] The picture just changed and is now still. If the image clearly SHOWS the current step '
         'finished, set "done_seen": true and give the next step. If I did something wrong, warn me. '
         'Otherwise reply with "say": "" and "overlay": null.')


class Phone:
    """Plays the iOS app over /hwmode/ws and keeps an ordered event log."""

    def __init__(self, ws):
        self.ws = ws
        self.log: list[tuple[str, dict]] = []
        self.frame_id = 0
        self.t0 = time.monotonic()

    async def start(self, task: str) -> None:
        await self.ws.send(json.dumps({"type": "start", "task": task}))
        msg = json.loads(await asyncio.wait_for(self.ws.recv(), 60))
        assert msg["type"] == "ready", msg
        self.log.append(("ready", msg))

    async def frame(self, jpeg: bytes) -> None:
        self.frame_id += 1
        await self.ws.send(json.dumps({"type": "frame", "id": self.frame_id, "jpeg": base64.b64encode(jpeg).decode()}))

    async def ask(self, text: str, frame: bool = True) -> dict:
        kind = "watch" if text.startswith("[watch]") else "ask"
        self.log.append((kind, {"text": text[:60]}))
        await self.ws.send(json.dumps({"type": "ask", "text": text, "frame": frame}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), 90))
            if msg["type"] == "say":
                self.log.append(("say", msg))
            elif msg["type"] == "reply":
                d = msg["data"] or {}
                self.log.append(("reply", d))
                print(f"  {time.monotonic() - self.t0:5.1f}s {kind:5} {text[:34]!r:38} -> step {d.get('step')}/{d.get('of')}"
                      f" seen={d.get('done_seen')} overlay={'Y' if d.get('overlay') else '-'} say={str(d.get('say'))[:80]!r}")
                return msg
            elif msg["type"] == "error":
                raise AssertionError(f"gateway error: {msg}")


def run_live(gateway_server, body):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", max_size=None,
                                          **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await body(Phone(ws))
    asyncio.run(run())


def _box(reply: dict):
    o = (reply.get("data") or {}).get("overlay") or {}
    px = o.get("target_px")
    return tuple(px) if isinstance(px, list) and len(px) == 4 else None


# ---------------------------------------------------------------- (c) vision

VISION_CASES = [
    # (scene state, render options, question, ground-truth part)
    ("in_port1", {}, "Where is the SATA2 port? Point at it.", "SATA2"),
    ("in_port1", {}, "Where is the SATA cable plug right now? Point at it.", "connector"),
    ("unplugged", {}, "Where is the loose SATA cable plug? Point at it.", "connector"),
    ("unplugged", {"noise": 0.5, "brightness": 0.45, "seed": 3}, "Where is the SATA2 port? Point at it.", "SATA2"),
    ("in_port1", {"shift": (40, -30), "blur": 1.5}, "Where is the SATA3 port? Point at it.", "SATA3"),
    ("unplugged", {}, "وين منفذ SATA2؟ أشر عليه.", "SATA2"),  # Arabic question
]


@pytest.mark.parametrize("state,opts,question,part", VISION_CASES, ids=[f"{c[0]}-{c[3]}-{i}" for i, c in enumerate(VISION_CASES)])
def test_vision_pointer_lands_on_the_asked_part(gateway_server, state, opts, question, part):
    async def body(phone: Phone):
        await phone.start(f"{TASK}. {STEPS_HINT}")
        scene = render(state, **opts)
        await phone.frame(scene.jpeg)
        reply = await phone.ask(question)
        box = _box(reply)
        assert box, f"no overlay for {question!r}: {reply['data']}"
        truth = scene.boxes[part]
        print(f"  box={box} truth={truth} iou={iou(box, truth):.2f}")
        assert (reply["w"], reply["h"]) == (1024, 768)
        assert center_in(box, truth), f"pointer center misses {part}: box={box} truth={truth}"
    run_live(gateway_server, body)


# ---------------------------------------------------------------- (d) step gating

def test_step1_not_done_stays_on_step1(gateway_server):
    async def body(phone: Phone):
        await phone.start(f"{TASK}. {STEPS_HINT}")
        await phone.frame(render("in_port1").jpeg)
        assert (await phone.ask(PLAN))["data"]["step"] == 1
        for k, opts in enumerate([{"shift": (12, 8)}, {"shift": (-10, 5), "noise": 0.2, "seed": 1}]):
            await phone.frame(render("in_port1", **opts).jpeg)  # moved, but still plugged into SATA1
            assert (await phone.ask(WATCH))["data"]["step"] == 1, f"watch {k} advanced with step 1 not done"
        assert (await phone.ask("where is the SATA2 port"))["data"]["step"] == 1
        assert (await phone.ask("is it ok to pull it now?"))["data"]["step"] == 1
    run_live(gateway_server, body)


def test_step1_seen_done_advances_to_step2(gateway_server):
    async def body(phone: Phone):
        await phone.start(f"{TASK}. {STEPS_HINT}")
        await phone.frame(render("in_port1").jpeg)
        assert (await phone.ask(PLAN))["data"]["step"] == 1
        await phone.frame(render("unplugged").jpeg)
        d = (await phone.ask(WATCH))["data"]
        assert d["step"] == 2 and d.get("done_seen") is True, d
        assert d.get("say"), "advancing must tell the user the next step"
    run_live(gateway_server, body)


def test_user_done_advances_exactly_one_step(gateway_server):
    async def body(phone: Phone):
        await phone.start(f"{TASK}. {STEPS_HINT}")
        await phone.frame(render("in_port1").jpeg)
        assert (await phone.ask(PLAN))["data"]["step"] == 1
        await phone.frame(render("unplugged").jpeg)
        assert (await phone.ask("done"))["data"]["step"] == 2
    run_live(gateway_server, body)


def test_wrong_port_gets_a_correction_and_no_advance(gateway_server):
    async def body(phone: Phone):
        await phone.start(f"{TASK}. {STEPS_HINT}")
        await phone.frame(render("unplugged").jpeg)
        assert (await phone.ask(PLAN))["data"]["step"] == 1
        assert (await phone.ask("done"))["data"]["step"] == 2
        await phone.frame(render("in_port3").jpeg)  # plugged into the WRONG port
        d = (await phone.ask(WATCH))["data"]
        assert d["step"] == 2, f"wrong port advanced: {d}"
        assert d.get("done_seen") is not True
        say = (d.get("say") or "").lower()
        assert say and ("sata2" in say or "wrong" in say or "sata 2" in say), f"no correction: {d}"
    run_live(gateway_server, body)


# ---------------------------------------------------------------- (e) full-flow replay

def test_full_repair_replay_event_order(gateway_server):
    """Synthetic video (plugged -> unplugged -> in SATA2) plus a spoken script, start to done."""
    frames = video(["in_port1", "unplugged", "in_port2"], frames_per_state=3)
    script = {  # frame index -> what the user says (None: a watch check after the picture settles)
        0: PLAN, 1: "where is the cable", 2: None,
        3: None, 4: "where do I plug it in",
        6: None, 8: "is that everything?",
    }

    async def body(phone: Phone):
        await phone.start(f"{TASK}. {STEPS_HINT}")
        steps: list[tuple[str, int, bool]] = []  # (event, step, seen_done)
        for i, scene in enumerate(frames):
            await phone.frame(scene.jpeg)
            if i in script:
                text = script[i] or WATCH
                d = (await phone.ask(text))["data"]
                steps.append(("watch" if script[i] is None else "ask", d["step"], d.get("done_seen") is True))
        kinds = [k for k, _ in phone.log]
        assert kinds[0] == "ready"
        assert kinds.count("reply") == len(script)
        # every ask/watch is answered before the next is sent: ask|watch, [say], reply
        for a, b in zip(kinds, kinds[1:]):
            if a in ("ask", "watch"):
                assert b in ("say", "reply"), kinds
        seq = [s for _, s, _ in steps]
        print("  step sequence:", seq)
        assert seq == sorted(seq), f"step went backwards: {seq}"
        assert seq[:3] == [1, 1, 1], f"left step 1 before the plug was out: {seq}"
        for (kind, s, seen), prev in zip(steps[1:], seq):
            if s > prev:  # every advance came from a watch check that SAW the step done
                assert kind == "watch" and seen, steps
        assert seq[-1] == 2, f"did not reach the last step: {seq}"
        last = [d for k, d in phone.log if k == "reply"][-1]
        print("  final:", last.get("say"))
    run_live(gateway_server, body)
