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
import logging
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import websockets

from conftest import IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from hwmode_synth import MINIPC_TASK, STEPS_HINT, TASK, W, H, center_in, iou, render, render_minipc, video

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
                assert "!" not in str(d.get("say") or ""), f"Jarvis never exclaims: {d.get('say')!r}"
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


# ---------------------------------------------------------------- owner's case: mini PC, cables on the back

LOCATE = ('[locate] I lost the highlight. Point again at the same part ({label}) in this picture. '
          'If you see it, reply with its overlay and "say": "". If it is not in view, set overlay to null and say where to aim.')


def _area_share(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1]) / (W * H)


def test_minipc_plan_boxes_one_part_not_a_whole_edge(gateway_server):
    async def body(phone: Phone):
        await phone.start(MINIPC_TASK)
        scene = render_minipc()
        await phone.frame(scene.jpeg)
        reply = await phone.ask(PLAN)
        box = _box(reply)
        if box is None:  # a first step with nothing to touch may have no box; then it must say where to look
            assert reply["data"].get("say")
            return
        print(f"  plan box={box} share={_area_share(box):.3f}")
        assert _area_share(box) < 0.08, f"generic box over a wide area: {box}"
        assert any(center_in(box, t, margin=40) for t in scene.boxes.values()), f"box on no plug: {box}"
    run_live(gateway_server, body)


@pytest.mark.parametrize("question,part", [("Point at the power cable.", "power"),
                                           ("Where is the HDMI cable? Show me.", "hdmi"),
                                           ("where is the white cable", "usb")])
def test_minipc_points_at_the_named_cable(gateway_server, question, part):
    async def body(phone: Phone):
        await phone.start(MINIPC_TASK)
        scene = render_minipc(noise=0.2, seed=5)
        await phone.frame(scene.jpeg)
        await phone.ask(PLAN)
        reply = await phone.ask(question)
        box = _box(reply)
        assert box, f"no box for {question!r}: {reply['data']}"
        truth = scene.boxes[part]
        print(f"  box={box} truth={truth} iou={iou(box, truth):.2f}")
        assert center_in(box, truth, margin=30), f"pointer misses the {part} plug: {box} vs {truth}"
        assert reply["data"].get("say"), "a question must be answered out loud"
    run_live(gateway_server, body)


def test_minipc_locate_finds_the_plug_after_the_camera_moved(gateway_server):
    async def body(phone: Phone):
        await phone.start(MINIPC_TASK)
        await phone.frame(render_minipc().jpeg)
        await phone.ask(PLAN)
        first = await phone.ask("Point at the power cable.")
        assert _box(first)
        moved = render_minipc(shift=(-120, 90))  # the phone moved: everything shifted
        await phone.frame(moved.jpeg)
        label = (first["data"].get("overlay") or {}).get("label") or "power cable"
        reply = await phone.ask(LOCATE.format(label=label))
        box = _box(reply)
        assert box, f"locate gave no box: {reply['data']}"
        print(f"  relocated box={box} truth={moved.boxes['power']}")
        assert center_in(box, moved.boxes["power"], margin=30)
        assert reply["data"]["step"] == first["data"]["step"], "locate changed the step"
        assert not reply["data"].get("say"), "a found part needs no speech"
    run_live(gateway_server, body)


# ---------------------------------------------------------------- owner's round-4 feedback (build 1.0.118)

def _said(reply: dict) -> str:
    return str((reply.get("data") or {}).get("say") or "")


@pytest.mark.parametrize("question,screw", [("Point at the top left screw.", "screw_tl"),
                                            ("Which screw is at the bottom right? Show me.", "screw_br")])
def test_minipc_small_screw_is_boxed_tightly_with_the_zoom_pass(gateway_server, caplog, question, screw):
    caplog.set_level(logging.INFO, logger="gateway.hwmode")

    async def body(phone: Phone):
        await phone.start(MINIPC_TASK)
        scene = render_minipc(noise=0.1, seed=7)
        await phone.frame(scene.jpeg)
        await phone.ask(PLAN)
        reply = await phone.ask(question)
        box = _box(reply)
        assert box, f"no box for {question!r}: {reply['data']}"
        truth = scene.boxes[screw]
        print(f"  box={box} truth={truth} iou={iou(box, truth):.2f}")
        assert center_in(box, truth, margin=15), f"pointer misses the {screw}: {box} vs {truth}"
        assert "zoom refine" in caplog.text, "a small part must get the zoom second pass"
    run_live(gateway_server, body)


@pytest.mark.parametrize("finger,want", [("hdmi", "no"), ("power", "yes")])
def test_minipc_is_this_it_looks_at_the_finger(gateway_server, finger, want):
    async def body(phone: Phone):
        await phone.start(MINIPC_TASK)
        await phone.frame(render_minipc().jpeg)
        await phone.ask(PLAN)
        scene = render_minipc(finger=finger)
        await phone.frame(scene.jpeg)
        reply = await phone.ask("Is this the power cable?")
        said = _said(reply).lower()
        assert re.search(rf"\b{want}\b", said), f"expected {want!r} for a finger on {finger}: {said!r}"
        before = [d for k, d in phone.log if k == "reply"][-2]
        assert said != str(before.get("say") or "").lower(), "it repeated itself instead of answering"
        box = _box(reply)
        assert box, f"no box: {reply['data']}"
        print(f"  box={box} power={scene.boxes['power']}")
        assert center_in(box, scene.boxes["power"], margin=30), "the box must land on the real power plug"
    run_live(gateway_server, body)


def test_minipc_side_question_mid_step_is_answered_with_a_box(gateway_server):
    async def body(phone: Phone):
        await phone.start(MINIPC_TASK)
        scene = render_minipc()
        await phone.frame(scene.jpeg)
        step = (await phone.ask(PLAN))["data"]["step"]
        reply = await phone.ask("Wait, point at the bottom right screw.")
        box = _box(reply)
        assert box, f"side question ignored: {reply['data']}"
        assert center_in(box, scene.boxes["screw_br"], margin=15), f"box {box} not on the screw"
        assert reply["data"]["step"] == step, "a side question must not move the step"
        assert _said(reply), "a side question is answered out loud"
    run_live(gateway_server, body)


def test_go_back_one_step_by_voice(gateway_server):
    async def body(phone: Phone):
        await phone.start(f"{TASK}. {STEPS_HINT}")
        await phone.frame(render("in_port1").jpeg)
        assert (await phone.ask(PLAN))["data"]["step"] == 1
        await phone.frame(render("unplugged").jpeg)
        assert (await phone.ask("done"))["data"]["step"] == 2
        back = await phone.ask("go back one step")
        assert back["data"]["step"] == 1, back["data"]
        assert _said(back), "going back must repeat step 1 out loud"
    run_live(gateway_server, body)
