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
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import struct

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from gateway.ws import IOS_ROLE, _authorized
from hwmode.brain import LeanSession

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FRAME_BYTES = 4 * 1024 * 1024
# Restart the session after this many turns: every turn resends earlier images.
MAX_TURNS = 20
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

    async def send(self, msg: dict) -> None:
        await self.ws.send_text(json.dumps(msg))

    def _close_session(self) -> None:
        if self.session:
            self.session.close()
            self.session = None

    async def start(self, task: str, model: str) -> None:
        self._close_session()
        self.task, self.model = task, model if model in ("sonnet", "haiku") else "sonnet"
        self.session = await asyncio.to_thread(LeanSession, self.model, claude_bin=_claude_bin())
        await self.send({"type": "ready", "model": self.model})

    async def ask(self, text: str, use_frame: bool) -> None:
        async with self.busy:
            if self.session is None or self.session.turns >= MAX_TURNS:
                carry = f"Task: {self.task}. " if self.task else ""
                await self.start(self.task, self.model)
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
            data = base64.b64decode(msg.get("jpeg") or "", validate=False)
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
            # Answer in the background so frames keep flowing while the brain thinks.
            asyncio.create_task(self._ask_safely(text, bool(msg.get("frame", True))))
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

    def close(self) -> None:
        self._close_session()


@router.websocket("/hwmode/ws")
async def hwmode_ws(websocket: WebSocket) -> None:
    if not _authorized(websocket, IOS_ROLE):
        await websocket.close(code=1008)
        return
    await websocket.accept()
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
        await asyncio.to_thread(conn.close)
