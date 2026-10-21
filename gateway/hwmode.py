"""Hardware Mode: the phone streams camera frames, a lean Claude Code session answers.

`/hwmode/ws`, Bearer `ios_token` only. One brain session per connection; it
lives on this box (the mini PC) on the owner's own Claude login, and is only
asked when the phone sends `ask` — frames alone never trigger a model call.

Client -> server (JSON text or binary frames):
  {"type": "start", "task": str, "model"?: "sonnet"|"haiku"}  new session for a task
  {"type": "frame", "id": int, "jpeg": base64}                   keep as the latest frame
  {"type": "ask", "text": str, "frame"?: bool}                   ask about the latest frame
  {"type": "diag", "data": {...}}                                phone voice/audio state, logged only
Server -> client:
  {"type": "ready", "model": str}
  {"type": "say", "text": str}                                   early speech, before the full reply
  {"type": "reply", "data": {...}|null, "raw": str, "frame_id": int|null, "w": int, "h": int,
   "ttft_s": float|null, "total_s": float}
  {"type": "error", "error": str}

A Claude session that needs a physical action posts `/hwmode/request`
(shared token, from `jarvis-hw-request` on this box); the phone lists them
(`GET /hwmode/requests`, ios_token) and shows "Open Hardware Mode" on that
session's card, then clears it with `DELETE /hwmode/request/{session_id}`.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import shutil
import struct
import time

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from gateway.auth import require_bearer, require_bearer_any
from gateway.ws import IOS_ROLE, _authorized
from hwmode.brain import LeanSession

logger = logging.getLogger(__name__)
# uvicorn only configures its own loggers: send the hwmode trace (asks, replies,
# phone diagnostics) to stderr, i.e. the service journal.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("hwmode: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
router = APIRouter()

MAX_FRAME_BYTES = 4 * 1024 * 1024
# Restart the session after this many turns: every turn resends earlier images.
MAX_TURNS = 20
REQUEST_TTL_S = 12 * 3600
MAX_REQUESTS = 50
# Each live connection is one `claude` process on this box.
MAX_CONNECTIONS = 2
_live_conns = 0
# session_id -> {"task": str, "at": epoch seconds}; in memory: a gateway restart drops them.
_requests: dict[str, dict] = {}
_SAY_RE = re.compile(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)"')
_STEP_RE = re.compile(r'"step"\s*:\s*(\d+)')
WATCH_PREFIX = "[watch]"
# The user saying the current step is finished: the only spoken way to move on.
_DONE_RE = re.compile(
    r"(ok(ay)? |yes |alright |right )?"
    r"((i'?m|i am|it'?s|it is|that'?s|that is|all|step \w+( is)?) )?"
    r"(done|finished|complete|completed)( now)?"
    r"|(ok(ay)? )?next( step)?( please)?"
)


def is_done_confirmation(text: str) -> bool:
    """True for "done", "I'm finished", "next step"...; False for questions like "is it done?"."""
    t = re.sub(r"[^a-z' ]+", " ", text.lower().replace("\u2019", "'")).strip()
    t = re.sub(r"\s+", " ", t)
    return not text.strip().endswith("?") and _DONE_RE.fullmatch(t) is not None


class StepGate:
    """The step the guide is on. It may open step 1, stay, or go back freely, but it
    moves forward only when the current step is confirmed done, and then by exactly one."""

    def __init__(self) -> None:
        self.current = 0

    def allow(self, step: object, confirmed: bool) -> int:
        if isinstance(step, bool) or not isinstance(step, int) or step < 1:
            return self.current
        if self.current == 0:
            self.current = 1  # the plan opens step 1, even if the brain thinks it is already done
        elif step <= self.current:
            self.current = step
        elif confirmed:
            self.current += 1
        return self.current


def _claude_bin() -> str:
    return os.environ.get("HWMODE_CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker, without an image library."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


class _Conn:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.session: LeanSession | None = None
        self.model = "sonnet"
        self.task = ""
        self.frame: bytes | None = None
        self.frame_id: int | None = None
        self.busy = asyncio.Lock()
        self.gate = StepGate()
        # A user question that arrived while the brain was busy: answered right after, never dropped.
        self.pending: tuple[str, bool] | None = None
        self.tasks: set[asyncio.Task] = set()  # strong refs: asyncio keeps only weak ones

    async def send(self, msg: dict) -> None:
        await self.ws.send_text(json.dumps(msg))

    def _close_session(self) -> None:
        if self.session:
            self.session.close()
            self.session = None

    async def start(self, task: str, model: str) -> None:
        async with self.busy:
            await self._start(task, model, announce=True)
        logger.info("start model=%s task=%r", self.model, task[:200])

    async def _start(self, task: str, model: str, announce: bool) -> None:
        self._close_session()
        self.task, self.model = task, model if model in ("sonnet", "haiku") else "sonnet"
        if announce:  # a new task starts over; a silent restart (turn cap) keeps the step
            self.gate = StepGate()
        self.session = await asyncio.to_thread(LeanSession, self.model, claude_bin=_claude_bin())
        if announce:  # the phone answers "ready" by planning the task, so only for a real start
            await self.send({"type": "ready", "model": self.model})

    async def ask(self, text: str, use_frame: bool) -> None:
        async with self.busy:
            if self.session is None or self.session.turns >= MAX_TURNS:
                await self._start(self.task, self.model, announce=False)
            asked = text
            watch = asked.startswith(WATCH_PREFIX)
            user_done = not watch and is_done_confirmation(asked)
            if self.session.turns == 0 and self.task:  # a fresh brain must know the job
                where = f" We are on step {self.gate.current}." if self.gate.current else ""
                text = f"Task: {self.task}.{where} {text}"
            jpeg = self.frame if use_frame else None
            frame_id = self.frame_id if use_frame else None
            size = jpeg_size(jpeg) if jpeg else None
            loop = asyncio.get_running_loop()
            streamed: list[str] = []
            said = False

            def on_text(chunk: str) -> None:
                # Speak early only once the reply's step is known to pass the gate:
                # a refused jump must never be heard.
                nonlocal said
                streamed.append(chunk)
                if said:
                    return
                joined = "".join(streamed)
                m_step, m_say = _STEP_RE.search(joined), _SAY_RE.search(joined)
                if not (m_step and m_say):
                    return
                step, cur = int(m_step.group(1)), self.gate.current
                if cur == 0 or step <= cur or (user_done and step == cur + 1):
                    said = True
                    say = json.loads(f'"{m_say.group(1)}"')
                    asyncio.run_coroutine_threadsafe(self.send({"type": "say", "text": say}), loop)

            logger.info("ask frame=%s text=%r", frame_id, text[:200])
            reply = await asyncio.to_thread(self.session.ask, text, jpeg, on_text)
            data = self._gate(reply.data, user_done, watch, jpeg is not None)
            if data is not None and data.get("step") != (reply.data or {}).get("step") and not watch:
                # The brain tried to move on without a confirmed "done": ask it to stay.
                cur = self.gate.current
                logger.info("step gate: refused %s -> %s, re-asking", cur, (reply.data or {}).get("step"))
                again = (f"The user has NOT confirmed step {cur} is done. Stay on step {cur}"
                         f" and answer again: {asked}")
                said = True  # no early speech from the retry: the final reply carries it
                reply = await asyncio.to_thread(self.session.ask, again, jpeg, None)
                data = self._gate(reply.data, user_done, watch, jpeg is not None)
                if data is not None and data.get("step") != (reply.data or {}).get("step"):
                    # Still jumping: never speak the next step's instructions on this step.
                    data = {**data, "say": f"Let's finish step {cur} first. Say done when it is.", "overlay": None}
            d = data or {}
            logger.info("reply %.1fs step=%s/%s gate=%s say=%r", reply.total_s, d.get("step"), d.get("of"),
                        self.gate.current, str(d.get("say", reply.raw))[:200])
            await self.send({"type": "reply", "data": data, "raw": reply.raw, "frame_id": frame_id,
                             "w": size[0] if size else None, "h": size[1] if size else None,
                             "ttft_s": reply.ttft_s, "total_s": round(reply.total_s, 3)})

    def _gate(self, data: dict | None, user_done: bool, watch: bool, has_frame: bool) -> dict | None:
        """The reply as the phone may see it: its step can only move forward on a confirmed done."""
        if not isinstance(data, dict) or "step" not in data:
            return data
        seen_done = watch and has_frame and data.get("done_seen") is True
        want = data.get("step")
        got = self.gate.allow(want, confirmed=user_done or seen_done)
        if want == got:
            return data
        if watch:  # a watch check that could not prove the step is done: stay, and stay quiet
            return {**data, "step": got, "say": "", "overlay": None}
        return {**data, "step": got}

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "frame":
            try:
                data = base64.b64decode(str(msg.get("jpeg") or ""))
            except (binascii.Error, ValueError):
                data = b""
            if not data or len(data) > MAX_FRAME_BYTES or jpeg_size(data) is None:
                await self.send({"type": "error", "error": "bad frame"})
                return
            self.frame, self.frame_id = data, msg.get("id")
        elif kind == "start":
            await self.start(str(msg.get("task") or "")[:2000], str(msg.get("model") or "sonnet"))
        elif kind == "ask":
            text = str(msg.get("text") or "").strip()[:2000]
            if not text:
                await self.send({"type": "error", "error": "empty question"})
                return
            use_frame = bool(msg.get("frame", True))
            if self.tasks:  # one model turn at a time
                if text.startswith(WATCH_PREFIX):  # a background check can just be skipped
                    await self.send({"type": "error", "error": "busy"})
                    return
                self.pending = (text, use_frame)  # keep only the newest user question
                logger.info("ask queued while busy: %r", text[:200])
                return
            # Answer in the background so frames keep flowing while the brain thinks.
            t = asyncio.create_task(self._ask_safely(text, use_frame))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        elif kind == "diag":
            logger.info("diag %s", json.dumps(msg.get("data"), separators=(",", ":"))[:2000])
        else:
            await self.send({"type": "error", "error": f"unknown type {kind!r}"})

    async def _ask_safely(self, text: str, use_frame: bool) -> None:
        while True:
            try:
                await self.ask(text, use_frame)
            except Exception as exc:  # brain crash must not kill the socket
                logger.exception("hwmode: ask failed")
                self._close_session()
                try:
                    await self.send({"type": "error", "error": f"brain error: {type(exc).__name__}"})
                except Exception:
                    pass
            if self.pending is None:
                return
            (text, use_frame), self.pending = self.pending, None

    async def close(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.to_thread(self._close_session)


@router.websocket("/hwmode/ws")
async def hwmode_ws(websocket: WebSocket) -> None:
    global _live_conns
    if not _authorized(websocket, IOS_ROLE):
        await websocket.close(code=1008)
        return
    if _live_conns >= MAX_CONNECTIONS:
        await websocket.close(code=1013)  # try again later
        return
    await websocket.accept()
    _live_conns += 1
    conn = _Conn(websocket)
    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(code=frame.get("code", 1000))
            raw = frame.get("text")
            if raw is None and isinstance(frame.get("bytes"), (bytes, bytearray)):
                raw = frame["bytes"].decode("utf-8", errors="replace")
            if raw is None:
                continue
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError
            except ValueError:
                await conn.send({"type": "error", "error": "invalid message"})
                continue
            await conn.handle(msg)
    except WebSocketDisconnect:
        pass
    finally:
        _live_conns -= 1
        await conn.close()


class HardwareRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=1, max_length=2000)


def _live_requests() -> dict[str, dict]:
    cutoff = time.time() - REQUEST_TTL_S
    for sid in [sid for sid, r in _requests.items() if r["at"] < cutoff]:
        del _requests[sid]
    return _requests


@router.post("/hwmode/request", dependencies=[Depends(require_bearer)])
async def post_request(req: HardwareRequest, request: Request) -> dict:
    # Only the local CLI may post: tailnet callers arrive with their real address
    # (uvicorn --proxy-headers behind tailscale serve), so the shared token alone is not enough.
    if (request.client.host if request.client else None) not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="local callers only")
    live = _live_requests()
    if req.session_id not in live and len(live) >= MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="too many open requests")
    live[req.session_id] = {"task": req.task, "at": time.time()}
    return {"ok": True}


@router.get("/hwmode/requests", dependencies=[Depends(require_bearer_any("ios_token"))])
async def list_requests() -> dict:
    return {sid: r["task"] for sid, r in _live_requests().items()}


@router.delete("/hwmode/request/{session_id}", dependencies=[Depends(require_bearer_any("ios_token"))])
async def clear_request(session_id: str) -> dict:
    return {"ok": _live_requests().pop(session_id, None) is not None}
