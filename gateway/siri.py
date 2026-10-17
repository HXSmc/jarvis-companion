"""Siri Shortcut endpoint: POST /siri/hey — transcript in, reply out.

Included by gateway/app.py (this module defines `router`).
The iPhone Shortcut POSTs {"text": "<transcript>"} and speaks the returned
{"reply": "..."} value (docs/DESIGN.md "Feature data flows" #5, iPhone row).
A Shortcut call must answer in seconds: status phrases are answered straight
from the agent registry, and everything that isn't a local intent (status,
quick-ask, screen, wake) goes to the mini PC router's POST /route (via
gateway/central.py), hard-bounded by ROUTER_TIMEOUT_SECONDS. The router's
action/session_id/title ride along so the phone can open the session.
Only the iPhone app's `ios_token` may reach the router: the Shortcut's shared
token can never start a router session (C1) — instead it gets a real answer
from the terse quick-ask pipeline (gateway/quick.py: vault -> web -> brain),
bounded by SHARED_TOKEN_TIMEOUT_SECONDS so Siri never hangs.
"""

from __future__ import annotations

import asyncio
import collections
import re
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from common import config
from gateway import central, quick, screen, wake
from gateway.auth import _bearer_matches, ios_token_is_shared, require_bearer_any
from gateway.registry import roles
from mac.voice import router as voice_router

router = APIRouter()

# Hard cap on a router call; on expiry the endpoint returns the canned reply.
# Only the iPhone app (ios_token) reaches the router here, and it waits 30 s
# (App.swift ask()); the router's classify alone takes 8-25 s, so 15 s threw
# away direct answers that landed a few seconds later.
ROUTER_TIMEOUT_SECONDS = 27

# The router may still finish after our deadline, so never say "try again"
# (a retry with a new msg_id could start the task twice).
_TIMEOUT_REPLY = "Started, check the app."
# Same cap as central.ChatBody.text.
MAX_TEXT_CHARS = 4000
_ROUTER_DOWN_REPLY = "The mini PC router is unavailable, try again."
# The shared token can start nothing on the mini PC (C1): a general question
# instead goes through quick.ask (vault -> web -> brain, no router, no
# tools) — bounded tighter than quick.ask's own worst case (Jev 5 s + vault
# 20 s + web 40 s + brain 15 s) so a Shortcut run never hangs past Siri's
# own ~30 s ceiling.
SHARED_TOKEN_TIMEOUT_SECONDS = 25.0
_SHARED_TOKEN_TIMEOUT_REPLY = "That's taking a while, try asking again."
_SHARED_TOKEN_NO_ANSWER_REPLY = "I don't know. For tasks, use the Jarvis app."

# Each voice/home-screen turn opens its own router thread (so a new prompt never
# lands in the previous turn's session) unless the client names one; retries stay
# idempotent through msg_id, which the router dedupes before any thread lookup.
_THREAD_RE = re.compile(central.ID_PATTERN)

# At most 2 concurrent router calls — a third gets an immediate busy reply
# (no queueing: a Shortcut can't out-wait a queue). Names kept from the brain era.
_brain_semaphore = asyncio.Semaphore(2)

# Crude per-process rate limit: max 20 router calls per rolling 60 s window.
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


# Quick-ask prefixes (spec §18): "quick what…", "question who…", "q: how…",
# "? when…" → the terse gateway/quick.py pipeline. Matched case-insensitively
# on the stripped transcript; the prefix and its whitespace are stripped and
# the remainder goes to quick.ask. No brain slot is consumed — quick.py has
# its own budget.
_QUICK_PREFIX_RE = re.compile(r"^(?:(?:quick|question)\b|q:|\?)\s*", re.IGNORECASE)

_SCREEN_REPLIES = {
    "screen_mac": "Opening your Mac screen",
    "screen_windows": "Opening your PC screen",
    "screen_minipc": "Opening your Mini PC screen",
}

# Wake intents (spec §19), fullmatch on the stripped transcript: magic packet
# out, then the reply reports the online-wait result.
_WAKE_RE = re.compile(
    r"wake (up )?(my |the )?pc|turn on (my |the )?pc|start (my |the )?pc",
    re.IGNORECASE,
)


async def _router_reply(text: str, thread_id: str, msg_id: str) -> dict:
    """central.route (source "voice"), hard-bounded; failure → canned reply, never a 500.

    `reply` always holds something speakable: the router's reply, else its
    needs_input prompt, else the session title. action/session_id/title (and
    prompt/options on needs_input) are passed through for the phone.
    """
    try:
        data = await asyncio.wait_for(
            central.route(thread_id, msg_id, text, "voice"),
            timeout=ROUTER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"reply": _TIMEOUT_REPLY}
    except Exception:
        return {"reply": _ROUTER_DOWN_REPLY}
    out = {k: data[k] for k in ("action", "session_id", "kind", "title", "prompt", "options") if k in data}
    out["reply"] = str(data.get("reply") or data.get("prompt") or data.get("title") or "On it.")
    return out


# The Shortcut sends the shared token; the iPhone app sends `ios_token`.
@router.post("/siri/hey", dependencies=[Depends(require_bearer_any("token", "ios_token"))])
async def siri_hey(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """`{"text": "<transcript>", "thread_id"?: str, "msg_id"?: str}` → local intent or router → `{"reply": ...}`.

    A client `msg_id` (central.ID_PATTERN) is passed to the router so a retry is
    idempotent; without one each call gets a fresh id.

    The body is read off the Request (not a Pydantic param) so the Shortcut's
    POST works without a Content-Type header. Beyond 2 in-flight or 20/60 s
    router calls the answer is an immediate busy reply — never a queue.
    """
    try:
        body = await request.json()
        text = str(body.get("text") or "")
        thread_id = str(body.get("thread_id") or f"voice-{uuid.uuid4().hex}")
        msg_id = str(body.get("msg_id") or uuid.uuid4().hex)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail='expected JSON body {"text": "..."}')
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"text longer than {MAX_TEXT_CHARS} characters")
    if text.strip().lower() in _STATUS_PHRASES:
        return {"reply": _status_reply()}
    # Quick-ask prefixes: delegate to the terse /ask pipeline (never a brain slot).
    stripped = text.strip()
    quick_match = _QUICK_PREFIX_RE.match(stripped)
    if quick_match:
        try:
            answer = (await quick.ask(stripped[quick_match.end():])).answer
        except Exception:
            return {"reply": "quick ask failed, try again"}
        return {"reply": answer or ""}
    # Screen intents (SC7): launch the control page instead of a brain call —
    # the same launch_screen POST /screen/launch uses, never an HTTP self-call.
    cmd = voice_router.route(text)
    if cmd in _SCREEN_REPLIES:
        await screen.launch_screen(cmd[len("screen_"):])
        return {"reply": _SCREEN_REPLIES[cmd]}
    # Wake intents (§19): the online wait already happened inside wake_pc.
    if _WAKE_RE.fullmatch(stripped):
        try:
            result = await wake.wake_pc()
        except HTTPException as exc:
            if exc.status_code != 400:
                raise
            return {"reply": "no PC MAC configured"}
        if result["online"]:
            return {"reply": f"PC online in {round(result['waited_ms'] / 1000)} seconds"}
        if not result["sent"]:
            return {"reply": "PC wake failed to send — check the Mac and PC are on the same network"}
        return {"reply": "PC didn't come online after 90 seconds — check Wake-on-LAN is enabled"}
    # The dependency already let the bearer in; only ios_token goes on to the router
    # (never while ios_token equals the shared token: it would not prove the phone).
    # Every other caller here is the shared-token Shortcut — it never reaches
    # central.route; a general question gets a real answer from quick.ask instead.
    if ios_token_is_shared() or not _bearer_matches(authorization, config.get_token(name="ios_token")):
        try:
            result = await asyncio.wait_for(quick.ask(stripped), timeout=SHARED_TOKEN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"reply": _SHARED_TOKEN_TIMEOUT_REPLY}
        except Exception:
            return {"reply": _SHARED_TOKEN_NO_ANSWER_REPLY}
        return {"reply": result.answer or _SHARED_TOKEN_NO_ANSWER_REPLY}
    if not _THREAD_RE.fullmatch(thread_id):
        raise HTTPException(status_code=400, detail="invalid thread_id")
    if not _THREAD_RE.fullmatch(msg_id):
        raise HTTPException(status_code=400, detail="invalid msg_id")
    if _brain_semaphore.locked() or not _brain_slot_available():
        return {"reply": _BUSY_REPLY}
    async with _brain_semaphore:
        return await _router_reply(text, thread_id, msg_id)
