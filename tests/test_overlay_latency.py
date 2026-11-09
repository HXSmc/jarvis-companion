"""Overlay latency budget: ack p95 <= 300 ms, first answer audio p95 < 1000 ms.

40 turns through the real NDJSON endpoint with everything else stubbed: the ack
PCM is prerendered into the cache, the brain's first say line arrives after
150 ms, synth takes 50 ms. Measured from just before the request to the first
(ack) and second (answer) `audio` line on the stream.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import AUTH_HEADER, TOKEN
from common import config
from gateway import overlay
from mac.voice import acks, lean

ACK_PCM = b"\x00\x00" * 4800  # 0.2 s of silence at 24 kHz
RATE = 24000
TURNS = 40


class SlowBrain:
    """Streams its first (and only) say line 150 ms in, like a real session."""

    def __init__(self) -> None:
        self.turns = 0

    def ask(self, text, jpeg=None, on_text=None):
        self.turns += 1
        time.sleep(0.15)
        if on_text:
            on_text('{"say": "Answer line."}\n')
        return lean.Reply(raw="", ttft_s=0.15, total_s=0.2)

    def close(self):
        pass


class OneConversations:
    """Everyone shares the same session; drop() is a no-op."""

    def __init__(self, session) -> None:
        self._session = session

    def get(self, conv_id):
        return self._session

    def drop(self, conv_id):
        pass


def slow_synth(text):
    time.sleep(0.05)
    return b"\x00" * 100, RATE


def percentile(values: list[float], p: float) -> int:
    values = sorted(values)
    return round(values[max(0, math.ceil(p / 100 * len(values)) - 1)])


def test_overlay_latency(monkeypatch):
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": TOKEN)
    cache = acks.AckCache(lambda text: (ACK_PCM, RATE))
    cache.prerender()
    overlay.set_deps(overlay.OverlayDeps(
        acks=cache, synth=slow_synth,
        conversations=OneConversations(SlowBrain()),
        vlm_ask=lambda prompt, jpeg: '{"id": null}',
    ))
    try:
        app = FastAPI()
        app.include_router(overlay.router)
        client = TestClient(app, client=("127.0.0.1", 50000))
        ack_times: list[float] = []
        answer_times: list[float] = []
        for _ in range(TURNS):
            t0 = time.monotonic()
            audios = 0
            with client.stream("POST", "/overlay/utterance",
                               json={"text": "quick question", "conv_id": "lat"},
                               headers=AUTH_HEADER) as resp:
                for line in resp.iter_lines():
                    if line and json.loads(line).get("type") == "audio":
                        audios += 1
                        if audios == 1:
                            ack_times.append((time.monotonic() - t0) * 1000)
                        elif audios == 2:
                            answer_times.append((time.monotonic() - t0) * 1000)
                            break
        assert len(ack_times) == TURNS and len(answer_times) == TURNS

        ack_p50, ack_p95 = percentile(ack_times, 50), percentile(ack_times, 95)
        ans_p50, ans_p95 = percentile(answer_times, 50), percentile(answer_times, 95)
        print(f"ack_ms p50={ack_p50} p95={ack_p95}")
        print(f"first_answer_audio_ms p50={ans_p50} p95={ans_p95}")
        assert ack_p95 <= 300, f"ack p95 {ack_p95} ms > 300 ms"
        assert ans_p95 < 1000, f"first answer audio p95 {ans_p95} ms >= 1000 ms"
    finally:
        overlay.set_deps(None)
