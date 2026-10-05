"""Siri Shortcut endpoint: POST /siri/hey — transcript in, brain reply out.

Included by gateway/app.py (this module defines `router`).
The iPhone Shortcut POSTs {"text": "<transcript>"} and speaks the returned
{"reply": "..."} value (docs/DESIGN.md "Feature data flows" #5, iPhone row).
A Shortcut call must answer in seconds: status phrases are answered straight
from the agent registry (no brain call), and every brain call is hard-bounded
(BRAIN_TIMEOUT_SECONDS) so a slow `claude -p` fallback can never hang the request.
"""

from __future__ import annotations

import asyncio
import collections
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Request

from gateway import screen
from gateway.auth import require_bearer_any
from gateway.registry import roles
from mac.voice import brain
from mac.voice import router as voice_router

router = APIRouter()

# Dedicated pool: a timed-out brain thread (threads can't be killed) never
# blocks the loop's default-executor shutdown or starves its slots.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-brain")

# Hard cap on a brain call (`claude -p` has no timeout of its own); on expiry the
# endpoint returns the canned reply instead of hanging the Shortcut. It is passed
# down as the subprocess timeout too, so the claude process dies with the request.
BRAIN_TIMEOUT_SECONDS = 15

_TIMEOUT_REPLY = "Jarvis is taking too long, try again."

# At most 2 concurrent brain calls — a third gets an immediate busy reply
# (no queueing: a Shortcut can't out-wait a queue).
_brain_semaphore = asyncio.Semaphore(2)

# Crude per-process rate limit: max 20 brain calls per rolling 60 s window.
_BRAIN_MAX_CALLS = 20
_BRAIN_WINDOW_SECONDS = 60.0
_brain_call_times: collections.deque[float] = collections.deque()


def _brain_slot_available() -> bool:
    """Consume a rolling-window slot; False = rate limited (20 calls / 60 s)."""
    now = time.monotonic()
    while _brain_call_times and _brain_call_times[0] <= now - _BRAIN_WINDOW_SECONDS:
        _brain_call_times.popleft()
    if len(_brain_call_times) >= _BRAIN_MAX_CALLS:
        return False
    _brain_call_times.append(now)
    return True


_BUSY_REPLY = "Jarvis is busy, try again in a moment."

# Transcripts answered locally from the registry — no brain call, no subprocess.
_STATUS_PHRASES = frozenset({"status", "health", "ping", "are you there"})


def _status_reply() -> str:
    """"Gateway online" answer built from the live agent registry."""
    connected = roles()
    if connected:
        return f"Jarvis gateway online. Connected: {', '.join(connected)}"
    return "Jarvis gateway online. Connected: none"


async def _brain_reply(text: str) -> str:
    """brain.reply off the event loop, hard-bounded; timeout/failure → canned reply.

    The subprocess timeout equals the outer cap so the claude process itself is
    killed with the request, not just abandoned. The executor thread outlives a
    timeout (threads can't be killed) but its result is discarded — the endpoint
    itself always returns within the cap.
    """
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _executor, brain.reply, text, BRAIN_TIMEOUT_SECONDS
            ),
            timeout=BRAIN_TIMEOUT_SECONDS,
        )
    except Exception:
        # Timeout or any brain failure: the Shortcut gets an answer, never a 500.
        return _TIMEOUT_REPLY


# The Shortcut sends the shared token; the iPhone app sends `ios_token`.
@router.post("/siri/hey", dependencies=[Depends(require_bearer_any("token", "ios_token"))])
async def siri_hey(request: Request) -> dict[str, str]:
    """`{"text": "<transcript>"}` → registry/brain → `{"reply": "<reply>"}`.

    The body is read off the Request (not a Pydantic param) so the Shortcut's
    POST works without a Content-Type header. Beyond 2 in-flight or 20/60 s
    brain calls the answer is an immediate busy reply — never a queue.
    """
    try:
        text = str((await request.json()).get("text") or "")
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail='expected JSON body {"text": "..."}')
    if text.strip().lower() in _STATUS_PHRASES:
        return {"reply": _status_reply()}
    # Screen intents (SC7): launch the control page instead of a brain call —
    # the same launch_screen POST /screen/launch uses, never an HTTP self-call.
    cmd = voice_router.route(text)
    if cmd in ("screen_mac", "screen_windows"):
        await screen.launch_screen(cmd[len("screen_"):])
        return {"reply": "Opening your Mac screen" if cmd == "screen_mac" else "Opening your PC screen"}
    if _brain_semaphore.locked() or not _brain_slot_available():
        return {"reply": _BUSY_REPLY}
    async with _brain_semaphore:
        return {"reply": await _brain_reply(text)}
