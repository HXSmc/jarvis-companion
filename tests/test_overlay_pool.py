"""Overlay session pool: close/detach never blocks the event loop and never races.

The heartbeat tests run the real overlay router under uvicorn on the test's own
loop and read the NDJSON stream with async httpx, while a coroutine ticks every
20 ms on that same loop. A close that sleeps 0.5 s on the loop shows up as a gap.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
from fastapi import FastAPI

from conftest import AUTH_HEADER, TOKEN
from gateway import overlay
from mac.voice import acks, lean
from mac.voice.lean import Conversations

CLOSE_SECONDS = 0.5
MAX_GAP = 0.1


def wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cond()


class Stub:
    """A session whose close() is slow; counts its own closes."""

    def __init__(self, fail: bool = False, close_gate: threading.Event | None = None):
        self.fail = fail
        self.close_gate = close_gate
        self.closes = 0
        self.close_started = threading.Event()
        self.closed = threading.Event()
        self._lock = threading.Lock()

    def ask(self, text, jpeg=None, on_text=None):
        if self.fail:
            raise RuntimeError("claude process died")
        if on_text:
            on_text('{"say": "Answer line."}\n')
        return lean.Reply(raw="", ttft_s=0.0, total_s=0.0)

    def close(self):
        with self._lock:
            self.closes += 1
        self.close_started.set()
        if self.close_gate is not None:
            self.close_gate.wait(5)
        else:
            time.sleep(CLOSE_SECONDS)
        self.closed.set()


class Registry:
    """Factory that remembers every session it made."""

    def __init__(self, **kwargs):
        self.made: list[Stub] = []
        self._kwargs = kwargs
        self._lock = threading.Lock()

    def __call__(self):
        stub = Stub(**self._kwargs)
        with self._lock:
            self.made.append(stub)
        return stub


def install(monkeypatch, tmp_path, pool):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(overlay, "TOKEN_FILE", token_file)
    cache = acks.AckCache(lambda text: (b"\x00\x00" * 480, 24000))
    cache.prerender()
    overlay.set_deps(overlay.OverlayDeps(
        acks=cache, synth=lambda text: (b"\x00" * 100, 24000),
        conversations=pool, quiet_conversations=pool,
        vlm_ask=lambda prompt, jpeg: '{"id": null}',
    ))


async def run_turns_with_heartbeat(conv_ids, settled) -> float:
    """POST one turn per conv id on a live server; return the loop's max tick gap."""
    app = FastAPI()
    app.include_router(overlay.router)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            for conv_id in conv_ids:
                try:
                    async with client.stream(
                            "POST", "/overlay/utterance",
                            json={"text": "tell me something", "conv_id": conv_id},
                            headers=AUTH_HEADER) as resp:
                        async for _ in resp.aiter_lines():
                            pass
                except httpx.HTTPError:
                    pass  # the failing-ask stream may end abruptly
        deadline = time.monotonic() + 5
        while not settled() and time.monotonic() < deadline:  # keep ticking through the close
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await beat
        server.should_exit = True
        await serve_task
    return max(gaps)


def test_failed_ask_drop_does_not_block_the_loop(monkeypatch, tmp_path):
    reg = Registry(fail=True)
    pool = Conversations(reg)
    install(monkeypatch, tmp_path, pool)
    try:
        gap = asyncio.run(run_turns_with_heartbeat(
            ["a"], lambda: bool(reg.made) and reg.made[0].closed.is_set()))
    finally:
        overlay.set_deps(None)
    print(f"max event-loop gap (failed ask -> drop): {gap:.3f}s")
    assert reg.made[0].closes == 1
    assert gap < MAX_GAP


def test_eviction_does_not_block_the_loop(monkeypatch, tmp_path):
    reg = Registry()
    pool = Conversations(reg, max_sessions=1)
    install(monkeypatch, tmp_path, pool)
    try:
        gap = asyncio.run(run_turns_with_heartbeat(
            ["a", "b"], lambda: bool(reg.made) and reg.made[0].closed.is_set()))
    finally:
        overlay.set_deps(None)
    print(f"max event-loop gap (eviction): {gap:.3f}s")
    assert reg.made[0].closes == 1 and all(s.closes == 0 for s in reg.made[1:])
    assert gap < MAX_GAP


def test_drop_then_get_gives_a_new_session_and_closes_the_old_once():
    gate = threading.Event()
    reg = Registry(close_gate=gate)
    pool = Conversations(reg)
    old = pool.get("a")
    pool.drop("a")  # returns at once even though close() is blocked
    assert old.close_started.wait(5)
    new = pool.get("a")
    assert new is not old
    assert new.closes == 0
    gate.set()
    assert old.closed.wait(5)
    time.sleep(0.05)
    assert old.closes == 1
    assert new.closes == 0


def test_concurrent_gets_close_nothing_twice_and_respect_the_cap():
    reg = Registry(close_gate=threading.Event())
    pool = Conversations(reg, max_sessions=2)
    got: dict[str, Stub] = {}
    barrier = threading.Barrier(4)

    def worker(conv_id):
        barrier.wait(5)
        got[conv_id] = pool.get(conv_id)

    threads = [threading.Thread(target=worker, args=(f"c{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(got) == 4
    assert len({id(s) for s in got.values()}) == 4  # distinct sessions
    evicted = [s for s in got.values() if s.close_started.wait(0.5) or s.closes]
    assert len(evicted) == 2  # cap 2: two of the four were evicted
    for s in reg.made:
        s.close_gate.set()
    for s in evicted:
        assert s.closed.wait(5)
    time.sleep(0.05)
    assert all(s.closes <= 1 for s in reg.made)
    live = [s for s in got.values() if s.closes == 0]
    assert len(live) == 2
    spare = [s for s in reg.made if s not in got.values()]
    assert len(spare) <= 1 and all(s.closes == 0 for s in spare)


def test_same_new_id_from_two_threads_shares_one_session():
    reg = Registry()
    pool = Conversations(reg)
    barrier = threading.Barrier(2)
    out: list[Stub] = []

    def worker():
        barrier.wait(5)
        out.append(pool.get("same"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(out) == 2 and out[0] is out[1]
    assert len(reg.made) == 2  # the session for "same" + the next warm spare


def test_cancelled_turn_session_ends_up_closed(monkeypatch, tmp_path):
    started, release = threading.Event(), threading.Event()
    made: list[Stub] = []

    class Blocking(Stub):
        def ask(self, text, jpeg=None, on_text=None):
            started.set()
            release.wait(5)
            return lean.Reply(raw="", ttft_s=0.0, total_s=0.0)

    pool = Conversations(lambda: made.append(Blocking()) or made[-1])
    install(monkeypatch, tmp_path, pool)
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(overlay.router)
    client = TestClient(app, client=("127.0.0.1", 50000))
    try:
        with client.stream("POST", "/overlay/utterance",
                           json={"text": "tell me something", "conv_id": "x"},
                           headers=AUTH_HEADER) as resp:
            next(resp.iter_lines())
            assert started.wait(2)
        first = made[0]
        assert first.closed.wait(5)
        assert first.closes == 1
    finally:
        release.set()
        overlay.set_deps(None)
