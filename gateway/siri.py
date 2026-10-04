"""Siri Shortcut endpoint: POST /siri/hey — transcript in, brain reply out.

Included by gateway/app.py's guarded router seam (this module defines `router`).
The iPhone Shortcut POSTs {"text": "<transcript>"} and speaks the returned
{"reply": "..."} value (docs/DESIGN.md "Feature data flows" #5, iPhone row).
A Shortcut call must answer in seconds: status phrases are answered straight
from the agent registry (no brain call), and every brain call is hard-bounded
(BRAIN_TIMEOUT_SECONDS) so a slow `claude -p` fallback can never hang the request.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Request

from gateway.auth import require_bearer
from mac.voice import brain

router = APIRouter()

# Dedicated pool: a timed-out brain thread (threads can't be killed) never
# blocks the loop's default-executor shutdown or starves its slots.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-brain")

# Hard cap on a brain call (`claude -p` has no timeout of its own); on expiry the
# endpoint returns the canned reply instead of hanging the Shortcut.
BRAIN_TIMEOUT_SECONDS = 15

_TIMEOUT_REPLY = "Jarvis is taking too long, try again."

# Transcripts answered locally from the registry — no brain call, no subprocess.
_STATUS_PHRASES = frozenset({"status", "health", "ping", "are you there"})


def _status_reply() -> str:
    """"Gateway online" answer built from the live agent registry.

    The registry ships with T1.2 (not on this branch yet — resolves at merge);
    until then, report no connected roles rather than failing the endpoint.
    """
    try:
        from gateway.registry import roles
    except ImportError:
        connected: list[str] = []
    else:
        connected = roles()
    if connected:
        return f"Jarvis gateway online. Connected: {', '.join(connected)}"
    return "Jarvis gateway online. Connected: none"


async def _brain_reply(text: str) -> str:
    """brain.reply off the event loop, hard-bounded; timeout/failure → canned reply.

    The executor thread outlives a timeout (threads can't be killed) but its
    result is discarded — the endpoint itself always returns within the cap.
    """
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(_executor, brain.reply, text),
            timeout=BRAIN_TIMEOUT_SECONDS,
        )
    except Exception:
        # Timeout or any brain failure: the Shortcut gets an answer, never a 500.
        return _TIMEOUT_REPLY


@router.post("/siri/hey", dependencies=[Depends(require_bearer)])
async def siri_hey(request: Request) -> dict[str, str]:
    """`{"text": "<transcript>"}` → registry/brain → `{"reply": "<reply>"}`.

    The body is read off the Request (not a Pydantic param) so the Shortcut's
    POST works without a Content-Type header.
    """
    try:
        text = str((await request.json()).get("text") or "")
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail='expected JSON body {"text": "..."}')
    if text.strip().lower() in _STATUS_PHRASES:
        return {"reply": _status_reply()}
    return {"reply": await _brain_reply(text)}
