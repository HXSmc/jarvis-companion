"""Hardware Mode voice + small-part zoom on the gateway side (no model calls, no real TTS)."""

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
from gateway import hwmode
from hwmode.brain import Reply
from hwmode.tts import Voice

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    reply = json.dumps({"step": 1, "of": 2, "done_seen": False, "say": "The power cable, sir: left plug.", "overlay": None})
    for part in (reply[:30], reply[30:]):
        print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": part}}}), flush=True)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''


class FakeVoice:
    name = "bm_daniel"

    def speaks(self, text):
        return Voice.speaks(text)

    def synth(self, text):
        return b"\x01\x00" * 2400, 24000


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HWMODE_CLAUDE_BIN", str(path))
    return path


def test_voice_only_speaks_english_lines():
    assert Voice.speaks("Step one, sir.")
    assert not Voice.speaks("")
    assert not Voice.speaks("افصل الكيبل")  # the phone's Arabic voice takes these


def test_ready_announces_the_voice_and_each_line_comes_with_audio(gateway_server, fake_claude, monkeypatch):
    monkeypatch.setattr(hwmode, "get_voice", lambda: FakeVoice())

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                assert json.loads(await ws.recv()) == {"type": "ready", "model": "sonnet", "voice": "bm_daniel"}
                await ws.send(json.dumps({"type": "ask", "text": "where is the power cable", "frame": False}))
                msgs = [json.loads(await asyncio.wait_for(ws.recv(), 10)) for _ in range(3)]
                kinds = [m["type"] for m in msgs]
                assert sorted(kinds) == ["audio", "reply", "say"]
                assert kinds.index("say") < kinds.index("audio")  # text first, voice right after
                audio = next(m for m in msgs if m["type"] == "audio")
                assert audio["text"] == "The power cable, sir: left plug." and audio["rate"] == 24000
                assert len(base64.b64decode(audio["pcm"])) == 4800
                with pytest.raises(asyncio.TimeoutError):  # voiced once, not again for the full reply
                    await asyncio.wait_for(ws.recv(), 1)
    asyncio.run(run())


def test_no_voice_installed_means_the_phone_speaks(gateway_server, fake_claude, monkeypatch):
    monkeypatch.setattr(hwmode, "get_voice", lambda: None)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                assert json.loads(await ws.recv())["voice"] is None
    asyncio.run(run())


class ZoomSession:
    """Answers the zoom pass: the part sits at (300,300)-(468,468) in the 768 px crop."""

    def __init__(self):
        self.asked = []

    def ask(self, text, jpeg=None, on_text=None):
        self.asked.append((text, jpeg))
        return Reply(data={"step": 1, "overlay": {"target_px": [300, 300, 468, 468]}}, raw="", ttft_s=None, total_s=0.1)


def _jpeg(w=1024, h=768) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 40, 40)).save(buf, "JPEG")
    return buf.getvalue()


def test_small_box_gets_a_zoomed_second_look():
    pytest.importorskip("PIL.Image")
    conn = hwmode._Conn(ws=None)
    conn.session = ZoomSession()
    data = {"step": 1, "overlay": {"target_px": [500, 400, 520, 420], "label": "corner screw"}}  # 20 px: small
    out = asyncio.run(conn._refine_small(data, _jpeg()))
    text, crop = conn.session.asked[0]
    assert text.startswith("[zoom]") and "corner screw" in text and crop
    # crop = 200x200 around the box centre (510,410) -> origin (410,310), scale 768/200 = 3.84
    fine = out["overlay"]["target_px"]
    assert fine == pytest.approx([410 + 300 / 3.84, 310 + 300 / 3.84, 410 + 468 / 3.84, 310 + 468 / 3.84])
    assert out["overlay"]["label"] == "corner screw" and out["step"] == 1


def test_big_box_is_left_alone():
    conn = hwmode._Conn(ws=None)
    conn.session = ZoomSession()
    data = {"step": 1, "overlay": {"target_px": [100, 100, 600, 500]}}
    assert asyncio.run(conn._refine_small(data, _jpeg())) == data
    assert conn.session.asked == []
