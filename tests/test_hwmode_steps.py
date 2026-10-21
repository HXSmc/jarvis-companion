"""Hardware Mode step gating: the guide cannot leave a step until it is confirmed done
(the user says so, or a watch check SEES it finished in a frame). Never on a plain
model turn, a question, or a timer-driven watch check without proof."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import websockets

from conftest import IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from gateway.hwmode import StepGate, is_done_confirmation


@pytest.mark.parametrize("text", ["done", "Done.", "ok done", "I'm done", "finished", "next", "next step",
                                  "it's done", "that's done", "all done", "step one done",
                                  "خلصت", "تم", "خلاص", "انتهيت", "التالي", "الخطوة التالية", "خلصت."])
def test_done_confirmations(text):
    assert is_done_confirmation(text)


@pytest.mark.parametrize("text", ["where is the cable", "is it done?", "not done yet", "how do I know when it's done",
                                  "what's next?", "[watch] the picture changed", "", "undone",
                                  "خلصت؟", "وين الكيبل", "ما خلصت", "تم ايش؟"])
def test_not_done_confirmations(text):
    assert not is_done_confirmation(text)


def test_gate_starts_on_the_first_planned_step():
    g = StepGate()
    assert g.allow(1, confirmed=False) == 1  # the plan itself may open step 1


def test_gate_blocks_advance_without_confirmation():
    g = StepGate()
    g.allow(1, confirmed=False)
    assert g.allow(2, confirmed=False) == 1
    assert g.allow(4, confirmed=False) == 1
    assert g.current == 1


def test_gate_advances_one_step_on_confirmation():
    g = StepGate()
    g.allow(1, confirmed=False)
    assert g.allow(3, confirmed=True) == 2  # never skips past the next step
    assert g.allow(2, confirmed=False) == 2  # staying is always fine
    assert g.allow(1, confirmed=False) == 1  # going back is always fine


def test_gate_ignores_missing_step():
    g = StepGate()
    g.allow(1, confirmed=False)
    assert g.allow(None, confirmed=False) == 1
    assert g.allow(0, confirmed=True) == 1


# Fake `claude`: answers step 1 on the first turn, then always TRIES to jump to step 2,
# unless told the user has not confirmed (the gateway's re-ask), when it stays on step 1.
# done_seen is true only for a [watch] check that carries an image.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
turn = 0
for line in sys.stdin:
    turn += 1
    content = json.loads(line)["message"]["content"]
    has_img = any(c.get("type") == "image" for c in content)
    text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
    if turn == 1:
        d = {"say": "Step 1: unplug the power cable.", "step": 1, "of": 3, "overlay": None}
    elif "NOT confirmed" in text:
        d = {"say": "Finish step 1 first: unplug the power cable.", "step": 1, "of": 3, "overlay": None}
    else:
        d = {"say": "Step 2: remove the side panel.", "step": 2, "of": 3, "overlay": None,
             "done_seen": "[watch]" in text and has_img}
    reply = json.dumps(d)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HWMODE_CLAUDE_BIN", str(path))
    return path


def _jpeg() -> str:
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 10, 10)).save(buf, "JPEG")  # synthetic, not a real camera frame
    return base64.b64encode(buf.getvalue()).decode()


async def _reply(ws) -> dict:
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if msg["type"] == "reply":
            return msg["data"]


def test_ws_step_only_advances_on_done_or_seen_done(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "reseat the SATA cable"}))
                assert json.loads(await ws.recv())["type"] == "ready"

                async def ask(text, frame=False):
                    await ws.send(json.dumps({"type": "ask", "text": text, "frame": frame}))
                    return await _reply(ws)

                assert (await ask("Plan the steps, then guide me through step 1."))["step"] == 1
                # A question: the brain tries to jump, the gate re-asks, the guide stays on step 1.
                d = await ask("where is the cable")
                assert (d["step"], d["say"]) == (1, "Finish step 1 first: unplug the power cable.")
                # A watch check with no picture cannot prove anything: stays, and stays quiet.
                d = await ask("[watch] the picture changed", frame=False)
                assert (d["step"], d["say"]) == (1, "")
                # The user's confirmation moves exactly one step.
                assert (await ask("done"))["step"] == 2
    asyncio.run(run())


def test_ws_watch_check_that_sees_the_step_done_may_advance(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "plan", "frame": False}))
                assert (await _reply(ws))["step"] == 1
                await ws.send(json.dumps({"type": "frame", "id": 1, "jpeg": _jpeg()}))
                await ws.send(json.dumps({"type": "ask", "text": "[watch] the picture changed", "frame": True}))
                assert (await _reply(ws))["step"] == 2
    asyncio.run(run())


def test_ws_locate_moves_the_box_but_never_the_step(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "plan", "frame": False}))
                assert (await _reply(ws))["step"] == 1
                await ws.send(json.dumps({"type": "ask", "text": "[locate] I lost the highlight.", "frame": False}))
                d = await _reply(ws)
                # the fake brain tries to jump to step 2 with next-step speech: no jump, no speech, no re-ask
                assert (d["step"], d["say"]) == (1, "")
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 1)
    asyncio.run(run())


def test_ws_locate_while_busy_is_skipped(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "plan", "frame": False}))
                await ws.send(json.dumps({"type": "ask", "text": "[locate] lost it", "frame": False}))
                kinds = [json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] for _ in range(2)]
                assert sorted(kinds) == ["error", "reply"]
    asyncio.run(run())
