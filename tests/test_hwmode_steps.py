"""Hardware Mode instruction gating: a new instruction may be given only when the user
confirmed the current one done (they say so, or a watch check SEES it finished in a
frame). Never on a plain model turn, a question, or a watch check without proof."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import websockets

from conftest import IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from gateway.hwmode import InstructionGate, is_done_confirmation, step_navigation


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


def test_gate_allows_a_new_instruction_only_on_confirmation():
    g = InstructionGate()
    assert g.count == 0
    assert not g.allow(next_ready=True, confirmed=False)  # the brain's own jump is never enough
    assert not g.allow(next_ready=False, confirmed=True)  # a confirmation without a new instruction
    assert g.count == 0
    assert g.allow(next_ready=True, confirmed=True)
    assert g.count == 0  # the count is only fed by the connection, not by allow() itself


# Fake `claude`: gives the first instruction on turn 1, then always TRIES to jump to a new
# one, unless told the user has not confirmed (the gateway's re-ask), when it stays.
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
        d = {"next_ready": False, "done_seen": False, "say": "Unplug the power cable first.", "shape": None}
    elif "NOT confirmed" in text:
        d = {"next_ready": False, "done_seen": False, "say": "Finish this one first: the power cable.", "shape": None}
    else:
        d = {"next_ready": True, "done_seen": "[watch]" in text and has_img,
             "say": "Now remove the side panel.", "shape": None}
    print(json.dumps({"type": "result", "result": json.dumps(d), "usage": {}}), flush=True)
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


def test_ws_new_instruction_only_on_done_or_seen_done(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "reseat the SATA cable"}))
                assert json.loads(await ws.recv())["type"] == "ready"

                async def ask(text, frame=False):
                    await ws.send(json.dumps({"type": "ask", "text": text, "frame": frame}))
                    return await _reply(ws)

                assert (await ask("guide me"))["say"] == "Unplug the power cable first."
                # A question: the brain tries to jump, the gate re-asks, the guide stays on it.
                d = await ask("where is the cable")
                assert (d["next_ready"], d["say"]) == (False, "Finish this one first: the power cable.")
                # A watch check with no picture cannot prove anything: stays, and stays quiet.
                d = await ask("[watch] the picture changed", frame=False)
                assert (d["next_ready"], d["say"], d["shape"]) == (False, "", None)
                # The user's confirmation is the only spoken way to move on.
                d = await ask("done")
                assert (d["next_ready"], d["say"]) == (True, "Now remove the side panel.")
    asyncio.run(run())


def test_ws_watch_check_that_sees_it_done_may_advance(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "guide me", "frame": False}))
                assert (await _reply(ws))["say"] == "Unplug the power cable first."
                await ws.send(json.dumps({"type": "frame", "id": 1, "jpeg": _jpeg()}))
                await ws.send(json.dumps({"type": "ask", "text": "[watch] the picture changed", "frame": True}))
                d = await _reply(ws)
                assert d["next_ready"] is True and d["done_seen"] is True
    asyncio.run(run())


def test_ws_locate_moves_the_box_but_never_the_instruction(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "guide me", "frame": False}))
                assert (await _reply(ws))["say"] == "Unplug the power cable first."
                await ws.send(json.dumps({"type": "ask", "text": "[locate] I lost the highlight.", "frame": False}))
                d = await _reply(ws)
                # the fake brain tries to jump with next-instruction speech: no jump, no speech, no re-ask
                assert (d["next_ready"], d["say"]) == (False, "")
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 1)
    asyncio.run(run())


def test_ws_locate_while_busy_is_skipped(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "guide me", "frame": False}))
                await ws.send(json.dumps({"type": "ask", "text": "[locate] lost it", "frame": False}))
                kinds = [json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] for _ in range(2)]
                assert sorted(kinds) == ["error", "reply"]
    asyncio.run(run())


@pytest.mark.parametrize("text,want", [
    ("go back", -1), ("Go back one step.", -1), ("previous step", -1), ("back one step please", -1), ("ارجع", -1),
    ("الخطوة السابقة", -1), ("skip", 1), ("skip this step", 1), ("move on", 1), ("go forward one step", 1), ("تخطى", 1),
    ("can we go back?", 0), ("where is the cable", 0), ("done", 0), ("go back to the shop and buy screws", 0),
])
def test_step_navigation(text, want):
    assert step_navigation(text) == want


def test_ws_go_back_and_skip(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()

                async def ask(text):
                    await ws.send(json.dumps({"type": "ask", "text": text, "frame": False}))
                    return await _reply(ws)

                assert (await ask("guide me"))["say"] == "Unplug the power cable first."
                assert (await ask("skip this step"))["next_ready"] is True  # skip is an explicit move on
                d = await ask("go back one step")  # the brain jumps forward: the gate allows the re-give
                assert d["next_ready"] is True
                d = await ask("where is the cable")  # and holds there without a confirmation
                assert (d["next_ready"], d["say"]) == (False, "Finish this one first: the power cable.")
    asyncio.run(run())


def _keys(obj) -> set:
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v)
    return out


def test_no_step_numbers_on_the_wire(gateway_server, fake_claude):
    """Plan, question, watch, done, back: nothing the server sends ever counts instructions."""
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "reseat the SATA cable"}))
                msgs = [json.loads(await ws.recv())]
                for text in ("guide me", "where is the cable", "[watch] still good?",
                             "done", "go back one step"):
                    await ws.send(json.dumps({"type": "ask", "text": text, "frame": False}))
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                        msgs.append(msg)
                        if msg["type"] == "reply":
                            break
                return msgs
    msgs = asyncio.run(run())
    assert not _keys(msgs) & {"step", "of"}  # no message counts instructions anywhere


def test_say_never_counts_instructions(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                said = []
                for text in ("guide me", "where is the cable", "done", "go back one step"):
                    await ws.send(json.dumps({"type": "ask", "text": text, "frame": False}))
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                        if msg["type"] == "say":
                            said.append(msg["text"])
                        if msg["type"] == "reply":
                            if msg["data"] and msg["data"]["say"]:
                                said.append(msg["data"]["say"])
                            break
                return said
    for line in asyncio.run(run()):
        assert not re.search(r"step \d+ (of|/) \d+", line, re.I), line
