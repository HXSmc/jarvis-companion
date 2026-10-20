"""Hardware Mode: the phone streams camera frames, a lean Claude Code session answers.

`/hwmode/ws`, Bearer `ios_token` only. One brain session per connection; it
lives on this box (the mini PC) on the owner's own Claude login, and is only
asked when the phone sends `ask` — frames alone never trigger a model call.

Client -> server (JSON text or binary frames):
  {"type": "start", "task": str, "model"?: "sonnet"|"haiku"}  new session for a task
  {"type": "frame", "id": int, "jpeg": base64}                   keep as the latest frame
  {"type": "ask", "text": str, "frame"?: bool}                   ask about the latest frame
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

    async def _start(self, task: str, model: str, announce: bool) -> None:
        self._close_session()
        self.task, self.model = task, model if model in ("sonnet", "haiku") else "sonnet"
        self.session = await asyncio.to_thread(LeanSession, self.model, claude_bin=_claude_bin())
        if announce:  # the phone answers "ready" by planning the task, so only for a real start
            await self.send({"type": "ready", "model": self.model})

    async def ask(self, text: str, use_frame: bool) -> None:
        async with self.busy:
            if self.session is None or self.session.turns >= MAX_TURNS:
                carry = f"Task: {self.task}. " if self.task else ""
                await self._start(self.task, self.model, announce=False)
                text = carry + text
            jpeg = self.frame if use_frame else None
            frame_id = self.frame_id if use_frame else None
            size = jpeg_size(jpeg) if jpeg else None
            loop = asyncio.get_running_loop()
            streamed: list[str] = []
            said = False

            def on_text(chunk: str) -> None:
                nonlocal said
                streamed.append(chunk)
                if not said:
                    m = _SAY_RE.search("".join(streamed))
                    if m:
                        said = True
                        say = json.loads(f'"{m.group(1)}"')
                        asyncio.run_coroutine_threadsafe(self.send({"type": "say", "text": say}), loop)

            reply = await asyncio.to_thread(self.session.ask, text, jpeg, on_text)
            await self.send({"type": "reply", "data": reply.data, "raw": reply.raw, "frame_id": frame_id,
                             "w": size[0] if size else None, "h": size[1] if size else None,
                             "ttft_s": reply.ttft_s, "total_s": round(reply.total_s, 3)})

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
            if self.tasks:  # one question at a time; a flood must not queue model turns
                await self.send({"type": "error", "error": "busy"})
                return
            # Answer in the background so frames keep flowing while the brain thinks.
            t = asyncio.create_task(self._ask_safely(text, bool(msg.get("frame", True))))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        else:
            await self.send({"type": "error", "error": f"unknown type {kind!r}"})

    async def _ask_safely(self, text: str, use_frame: bool) -> None:
        try:
            await self.ask(text, use_frame)
        except Exception as exc:  # brain crash must not kill the socket
            logger.exception("hwmode: ask failed")
            self._close_session()
            try:
                await self.send({"type": "error", "error": f"brain error: {type(exc).__name__}"})
            except Exception:
                pass

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
