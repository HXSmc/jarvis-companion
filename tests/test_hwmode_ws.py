"""/hwmode/ws: ios_token auth, frame + ask -> early `say` + full reply, using a fake claude CLI."""

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

from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from gateway.hwmode import jpeg_size

# Stands in for `claude -p --input-format stream-json ...`: answers every user line
# with two text deltas and a result, echoing whether an image block was attached.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    content = msg["message"]["content"]
    has_img = any(c.get("type") == "image" for c in content)
    text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
    reply = json.dumps({"say": "Press the latch.", "step": 1, "of": 2,
                        "overlay": {"target_px": [10, 20, 30, 40], "action": "press",
                                    "arrow_dir": [0, 1],
                                    "label": ("img" if has_img else "noimg") + ("+task" if text.startswith("Task: ") else "")}})
    half = len(reply) // 2
    for part in (reply[:half], reply[half:]):
        print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": part}}}), flush=True)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''


def _tiny_jpeg(w=64, h=48) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 90, 40)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HWMODE_CLAUDE_BIN", str(path))
    return path


def test_jpeg_size_reads_sof():
    assert jpeg_size(_tiny_jpeg(64, 48)) == (64, 48)
    assert jpeg_size(b"not a jpeg") is None


def test_shared_token_is_rejected(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: AUTH_HEADER}):
                    pass
    asyncio.run(run())


def test_frame_then_ask_streams_say_and_reply(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "unplug SATA"}))
                assert json.loads(await ws.recv()) == {"type": "ready", "model": "sonnet", "voice": None}
                jpeg = _tiny_jpeg(64, 48)
                # Binary frame, like the iOS URLSessionWebSocketTask sends.
                await ws.send(json.dumps({"type": "frame", "id": 7, "jpeg": base64.b64encode(jpeg).decode()}).encode())
                await ws.send(json.dumps({"type": "ask", "text": "where is the latch?"}))
                say = json.loads(await asyncio.wait_for(ws.recv(), 10))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert say == {"type": "say", "text": "Press the latch."}
                assert reply["type"] == "reply" and reply["frame_id"] == 7
                assert (reply["w"], reply["h"]) == (64, 48)
                assert reply["data"]["overlay"]["label"] == "img+task"  # first ask carries the task

                await ws.send(json.dumps({"type": "ask", "text": "and now?", "frame": False}))
                await ws.recv()  # say
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert reply["frame_id"] is None and reply["data"]["overlay"]["label"] == "noimg"

                await ws.send(json.dumps({"type": "frame", "id": 8, "jpeg": base64.b64encode(b"junk").decode()}))
                assert json.loads(await ws.recv()) == {"type": "error", "error": "bad frame"}
    asyncio.run(run())


def test_hardware_requests_round_trip(gateway_server):
    import httpx
    from gateway import hwmode
    hwmode._requests.clear()

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            body = {"session_id": "abc123", "task": "reseat the SATA cable on the SSD"}
            assert (await http.post("/hwmode/request", json=body, headers=IOS_AUTH_HEADER)).status_code == 401
            assert (await http.post("/hwmode/request", json=body, headers=AUTH_HEADER)).status_code == 200
            assert (await http.get("/hwmode/requests", headers=AUTH_HEADER)).status_code == 401
            r = await http.get("/hwmode/requests", headers=IOS_AUTH_HEADER)
            assert r.json() == {"abc123": "reseat the SATA cable on the SSD"}
            assert (await http.delete("/hwmode/request/abc123", headers=IOS_AUTH_HEADER)).json() == {"ok": True}
            assert (await http.get("/hwmode/requests", headers=IOS_AUTH_HEADER)).json() == {}
    asyncio.run(run())


def test_session_restart_after_max_turns_is_silent(gateway_server, fake_claude, monkeypatch):
    from gateway import hwmode
    monkeypatch.setattr(hwmode, "MAX_TURNS", 1)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                assert json.loads(await ws.recv())["type"] == "ready"
                for _ in range(2):  # second ask restarts the brain: no extra "ready" may appear
                    await ws.send(json.dumps({"type": "ask", "text": "q", "frame": False}))
                    kinds = [json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] for _ in range(2)]
                    assert kinds == ["say", "reply"]
    asyncio.run(run())


def test_watch_while_busy_is_refused_but_user_question_is_queued(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "one", "frame": False}))
                await ws.send(json.dumps({"type": "ask", "text": "[watch] all good?", "frame": False}))
                await ws.send(json.dumps({"type": "ask", "text": "where is the cable", "frame": False}))
                kinds = [json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] for _ in range(5)]
                # watch: refused; the user's question: answered after the first one, not dropped
                assert sorted(kinds) == ["error", "reply", "reply", "say", "say"]
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 1)
    asyncio.run(run())


def test_diag_is_logged_without_reply(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "diag", "data": {"engine": True, "mic": "granted"}}))
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                assert json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] == "ready"
    asyncio.run(run())


def test_request_cap(gateway_server, monkeypatch):
    import httpx
    from gateway import hwmode
    hwmode._requests.clear()
    monkeypatch.setattr(hwmode, "MAX_REQUESTS", 2)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            codes = [(await http.post("/hwmode/request", json={"session_id": f"s{i}", "task": "x"}, headers=AUTH_HEADER)).status_code
                     for i in range(3)]
            assert codes == [200, 200, 429]
    asyncio.run(run())


def test_hardware_request_is_filed_under_the_router_session_id(gateway_server, monkeypatch):
    """jarvis-hw-request sends the Claude session uuid; the phone's cards key on the router id."""
    import httpx
    from gateway import cards, hwmode
    hwmode._requests.clear()
    ids = {"dc82157b-0000-4000-8000-000000000001": "63a31002"}
    monkeypatch.setattr(cards, "resolve_session", lambda raw: ids.get(raw))

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            for sid in ("dc82157b-0000-4000-8000-000000000001", "unknown1"):  # unknown: kept as sent
                body = {"session_id": sid, "task": "find the SATA drive"}
                assert (await http.post("/hwmode/request", json=body, headers=AUTH_HEADER)).status_code == 200
            return (await http.get("/hwmode/requests", headers=IOS_AUTH_HEADER)).json()
    assert asyncio.run(run()) == {"63a31002": "find the SATA drive", "unknown1": "find the SATA drive"}
