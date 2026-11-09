"""Hardware Mode ask latency: with a brain that takes 2.5 s to first token, every ask
still gets its cached ack in under a second."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG

# A brain that thinks: 2.5 s before the first delta, then the reply.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys, time
for line in sys.stdin:
    time.sleep(2.5)
    reply = json.dumps({"next_ready": False, "done_seen": False, "say": "The latch, sir.", "shape": None})
    print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": reply}}}), flush=True)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''


class FakeVoice:
    name = "bm_daniel"

    def speaks(self, text):
        from hwmode.tts import Voice
        return Voice.speaks(text)

    def synth(self, text):
        return b"\x01\x00" * 2400, 24000


def test_every_ask_gets_its_ack_within_a_second(gateway_server, tmp_path, monkeypatch):
    import websockets

    from gateway import hwmode
    from hwmode.acks import get_acks

    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HWMODE_CLAUDE_BIN", str(path))
    voice = FakeVoice()
    # ONE instance: the ack cache is keyed on the voice object's identity.
    monkeypatch.setattr(hwmode, "get_voice", lambda: voice)
    get_acks(voice).ready.wait(30)  # clips render in the background before the first ask

    lines = []

    class LogCatcher(logging.Handler):  # hwmode's logger has propagate=False: caplog sees nothing
        def emit(self, record):
            lines.append(record.getMessage())

    catcher = LogCatcher()
    hwmode.logger.addHandler(catcher)
    try:
        async def run():
            async with gateway_server() as base:
                async with websockets.connect(f"ws://{base}/hwmode/ws",
                                              **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                    await ws.send(json.dumps({"type": "start", "task": "reseat the SATA cable"}))
                    assert json.loads(await ws.recv())["voice"] == "bm_daniel"
                    times = []
                    for _ in range(20):
                        t0 = time.monotonic()
                        await ws.send(json.dumps({"type": "ask", "text": "where is it?", "frame": False}))
                        first = None
                        while True:  # ask sent -> first ack-or-audio, then drain to the reply
                            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                            if msg["type"] in ("ack", "audio") and first is None:
                                first = (time.monotonic() - t0) * 1000
                            if msg["type"] == "reply":
                                break
                        times.append(first)
                    return times
        times = asyncio.run(run())
    finally:
        hwmode.logger.removeHandler(catcher)

    assert len(times) == 20 and all(t is not None for t in times)
    times.sort()
    p50, p95 = times[len(times) // 2], times[int(len(times) * 0.95)]
    print(f"ack_ms p50={p50:.0f} p95={p95:.0f}")
    assert p95 <= 1000
    assert any(l.startswith("timing ttft_s=") for l in lines), lines[-3:]
