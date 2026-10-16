"""H.264 screen stream (Phase 1 prototype): one WebSocket carries video down and input up.

Report: .orchestrator/reports/stream-phase1.md. Ideas taken from RustDesk
(re-implemented, no code copied): one encoder per viewer, keyframes only on
request, drop stale frames instead of queueing them, delay-based quality
control. The JPEG polling routes in gateway/screen.py stay as the fallback.

Phase 1 runs a LOCAL helper process (STREAM_HELPER_CMD, default
linux/stream_helper.py on STREAM_DISPLAY) so it can be measured on the mini PC
with a loopback gateway; input is injected locally with xdotool. Phase 2 moves
the helper next to each screen (minipc over tailnet, Mac Swift CLI, Windows
agent) behind the same wire protocol.

Helper stdout framing: <I len><B flags><d ts_ms> + Annex-B access unit.
Helper stdin: JSON lines {"keyframe": true} | {"qp": n} | {"fps": n}.
WS binary (server -> client): <B flags><d ts_ms> + access unit.
WS text (server -> client): {"type": "hello"|"ping"}; (client -> server):
{"type": "pong"|"keyframe"|"input"}. Same auth as /screen/minipc/ws
(screen._ws_authorized). Close codes: 1008 auth, 4004 session cap, 4005 no
helper for target, 4006 helper died.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shlex
import struct
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Request, Response, WebSocket
from pydantic import ValidationError

from gateway import screen

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/screen")

STREAM_HTML = Path(__file__).resolve().parent / "static" / "stream.html"
_HELPER = Path(__file__).resolve().parent.parent / "linux" / "stream_helper.py"

HELPER_HEADER = struct.Struct("<IBd")
WS_HEADER = struct.Struct("<Bd")
FLAG_KEY = 1
MAX_AU_BYTES = 8 * 1024 * 1024  # a corrupt length must not make us buffer forever
MAX_STREAM_SESSIONS = 2
BACKLOG_DROP = 3  # queued access units before we drop to the next keyframe
PING_INTERVAL = 1.0

QP_BASE, QP_MAX, QP_STEP_UP, QP_STEP_DOWN = 26, 45, 4, 2
FPS_BASE, FPS_MIN = 30, 5
BAD_DELAY_MS = 150.0  # RTT above the session minimum that counts as congestion
BAD_STREAK, GOOD_STREAK = 3, 5


def helper_cmd(target: str) -> list[str] | None:
    """Helper argv for a target, or None when this gateway cannot stream it."""
    if target != "minipc":  # shortcut: Mac/Windows helpers are Phase 2
        return None
    cmd = os.environ.get("STREAM_HELPER_CMD")
    return shlex.split(cmd) if cmd else ["/usr/bin/python3", str(_HELPER)]


def helper_env() -> dict[str, str]:
    env = dict(os.environ)
    env["DISPLAY"] = os.environ.get("STREAM_DISPLAY", os.environ.get("DISPLAY", ":1"))
    return env


class Qos:
    """Delay-based quality control: congestion raises QP first, then halves fps.

    Like RustDesk's TestDelay loop: compare each RTT against the session's best
    RTT. Recovery is slow (one step per GOOD_STREAK good pings), fps first.
    """

    def __init__(self) -> None:
        self.qp, self.fps = QP_BASE, FPS_BASE
        self.min_rtt = float("inf")
        self.bad = self.good = 0

    def update(self, rtt_ms: float) -> dict | None:
        self.min_rtt = min(self.min_rtt, rtt_ms)
        if rtt_ms - self.min_rtt > BAD_DELAY_MS:
            self.bad, self.good = self.bad + 1, 0
            if self.bad < BAD_STREAK:
                return None
            self.bad = 0
            if self.qp < QP_MAX:
                self.qp = min(QP_MAX, self.qp + QP_STEP_UP)
                return {"qp": self.qp}
            if self.fps > FPS_MIN:
                self.fps = max(FPS_MIN, self.fps // 2)
                return {"fps": self.fps}
            return None
        self.good, self.bad = self.good + 1, 0
        if self.good < GOOD_STREAK:
            return None
        self.good = 0
        if self.fps < FPS_BASE:
            self.fps = min(FPS_BASE, self.fps * 2)
            return {"fps": self.fps}
        if self.qp > QP_BASE:
            self.qp = max(QP_BASE, self.qp - QP_STEP_DOWN)
            return {"qp": self.qp}
        return None


class Backlog:
    """Access units waiting for the socket. Too many queued -> drop to the next keyframe."""

    def __init__(self) -> None:
        self.q: collections.deque[tuple[int, float, bytes]] = collections.deque()
        self.waiting_key = False
        self.event = asyncio.Event()

    def push(self, flags: int, ts: float, au: bytes) -> bool:
        """Queue one AU; True means the caller must ask the helper for a keyframe."""
        key = bool(flags & FLAG_KEY)
        if key:
            self.q.clear()  # a keyframe makes everything queued before it useless
            self.waiting_key = False
        elif self.waiting_key:
            return False
        elif len(self.q) >= BACKLOG_DROP:
            self.q.clear()
            self.waiting_key = True
            return True
        self.q.append((flags, ts, au))
        self.event.set()
        return False

    async def pop(self) -> tuple[int, float, bytes]:
        while not self.q:
            self.event.clear()
            await self.event.wait()
        return self.q.popleft()


async def read_au(stdout: asyncio.StreamReader) -> tuple[int, float, bytes]:
    length, flags, ts = HELPER_HEADER.unpack(await stdout.readexactly(HELPER_HEADER.size))
    if length > MAX_AU_BYTES:
        raise ValueError("access unit too large")
    return flags, ts, await stdout.readexactly(length)


_KEYSYMS = {
    "return": "Return", "esc": "Escape", "delete": "BackSpace", "tab": "Tab", "space": "space",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
    "pageup": "Prior", "pagedown": "Next", **{f"f{i}": f"F{i}" for i in range(1, 13)},
}
_MODS = {"cmd": "super", "ctrl": "ctrl", "alt": "alt", "shift": "shift"}


def xdotool_args(event: dict, width: int, height: int) -> list[str]:
    """One validated ScreenEvent -> xdotool argv tail (coords 0..1 -> pixels)."""
    t = event["type"]
    if t in ("move", "tap", "dblclick", "right", "drag"):
        x = str(round(event["x"] * (width - 1)))
        y = str(round(event["y"] * (height - 1)))
        move = ["mousemove", "--sync", x, y]
        if t == "move":
            return move
        if t == "tap":
            return move + ["click", "3" if event["button"] == "right" else "1"]
        if t == "dblclick":
            return move + ["click", "--repeat", "2", "1"]
        if t == "right":
            return move + ["click", "3"]
        # drag: press at (x, y), release at (x + dx, y + dy), as screen.html sends it
        x2 = min(max(event["x"] + (event.get("dx") or 0), 0), 1)
        y2 = min(max(event["y"] + (event.get("dy") or 0), 0), 1)
        end = ["mousemove", "--sync", str(round(x2 * (width - 1))), str(round(y2 * (height - 1)))]
        return move + ["mousedown", "1"] + end + ["mouseup", "1"]
    if t == "scroll":
        out: list[str] = []
        if event.get("dy"):
            out += ["click", "--repeat", "3", "5" if event["dy"] > 0 else "4"]
        if event.get("dx"):
            out += ["click", "--repeat", "3", "7" if event["dx"] > 0 else "6"]
        return out
    if t == "text":
        return ["type", "--delay", "0", "--", event["text"]]
    combo = "+".join([_MODS[m] for m in event["mods"]] + [_KEYSYMS[event["key"]]])
    return ["key", "--", combo]


async def _display_size(env: dict[str, str]) -> tuple[int, int]:
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "getdisplaygeometry", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    w, h = out.split()
    return int(w), int(h)


async def _inject(events: list[dict], env: dict[str, str], size: tuple[int, int]) -> None:
    argv = ["xdotool"]
    for event in events:
        argv += xdotool_args(event, *size)
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _stop_helper(proc: asyncio.subprocess.Process) -> None:
    """stdin EOF = EOS for the helper; kill after 2 s; read stdout to EOF so the pipe transport closes now."""
    try:
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 2.0)
    except Exception:
        proc.kill()
        await proc.wait()
    try:
        await asyncio.wait_for(proc.stdout.read(), 1.0)
    except Exception:
        pass


@router.get("/stream")
async def stream_page(request: Request) -> Response:
    kind = screen._require(request)
    return screen._page(STREAM_HTML.read_text(), kind)


_live = 0  # current concurrent stream sessions


@router.websocket("/{target}/stream")
async def stream_ws(ws: WebSocket, target: str) -> None:
    global _live
    if not await screen._ws_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    argv = helper_cmd(target)
    if argv is None:
        await ws.close(code=4005)
        return
    if _live >= MAX_STREAM_SESSIONS:
        await ws.close(code=4004)
        return
    _live += 1
    proc = None
    tasks: list[asyncio.Task] = []
    try:
        env = helper_env()
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr if os.environ.get("STREAM_DEBUG") else asyncio.subprocess.DEVNULL,
        )
        backlog = Backlog()
        qos = Qos()
        size: tuple[int, int] | None = None

        def control(msg: dict) -> None:
            proc.stdin.write((json.dumps(msg) + "\n").encode())

        async def from_helper() -> None:
            while True:
                flags, ts, au = await read_au(proc.stdout)
                if backlog.push(flags, ts, au):
                    control({"keyframe": True})

        async def to_client() -> None:
            while True:
                flags, ts, au = await backlog.pop()
                await ws.send_bytes(WS_HEADER.pack(flags, ts) + au)

        async def pinger() -> None:
            while True:
                await ws.send_text(json.dumps({"type": "ping", "t": time.monotonic() * 1000}))
                await asyncio.sleep(PING_INTERVAL)

        async def from_client() -> None:
            nonlocal size
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "pong" and isinstance(msg.get("t"), (int, float)):
                    change = qos.update(time.monotonic() * 1000 - msg["t"])
                    if change:
                        control(change)
                elif kind == "keyframe":
                    control({"keyframe": True})
                elif kind == "input":
                    try:
                        body = screen.InputBody.model_validate({"events": msg.get("events")})
                    except ValidationError:
                        continue
                    if not screen._input_slot_available(target, len(body.events)):
                        continue
                    events = [e.model_dump() for e in body.events]
                    # Audit trail: count + types only, never coordinates or text.
                    logger.info("stream input: %s %d events %s", target, len(events), [e["type"] for e in events])
                    if size is None:
                        size = await _display_size(env)
                    await _inject(events, env, size)

        await ws.send_text(json.dumps({"type": "hello", "target": target}))
        tasks = [asyncio.create_task(c()) for c in (from_helper, to_client, pinger, from_client)]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if tasks[0] in done and not tasks[0].cancelled() and tasks[0].exception() is not None:
            try:
                await ws.close(code=4006)
            except Exception:
                pass
        for task in done:
            if not task.cancelled():
                task.exception()
    finally:
        # Every exit path stops the tasks, ends the helper (stdin EOF = EOS,
        # then kill) and closes the socket.
        _live -= 1
        for task in tasks:
            task.cancel()
        if proc is not None:
            # Shielded: a cancelled handler (client gone, gateway shutdown)
            # must still reap the helper and its pipes.
            await asyncio.shield(_stop_helper(proc))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await ws.close()
        except Exception:
            pass
