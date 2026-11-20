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
from gateway import cards, central, notify, quick, screen, wake
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
# (a retry with a new msg_id could start the task twice); _deliver_late
# sends the answer on when it lands.
_TIMEOUT_REPLY = "Still thinking. The answer will come to the app and WhatsApp."
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

# The iPhone app's 3 screen chips (ios/.../App.swift, the screen chips in `chips`; tests/test_phone_chips.py keeps them in sync) send one of these
# verbatim — phone callers only launch the screen on an exact match, never on
# voice_router.route()'s keyword/substring matching (which also fires on any
# text containing "screen" plus "mac"/"pc"/"windows"/"computer", e.g. "fix the
# screen bug on my mac"). Matched case-insensitively on the stripped text.
_PHONE_SCREEN_CMDS = {
    "show mac screen": "screen_mac",
    "show pc screen": "screen_windows",
    "show mini pc screen": "screen_minipc",
}

# Wake intents (spec §19), fullmatch on the stripped transcript: magic packet
# out, then the reply reports the online-wait result.
_WAKE_RE = re.compile(
    r"wake (up )?(my |the )?pc|turn on (my |the )?pc|start (my |the )?pc",
    re.IGNORECASE,
)


# Threads known to carry a local-only chat (round 1, S2): the picker resets to Hub after
# each send, so a follow-up on a local thread may name no engine at all.
# thread_id -> its local engine (R4-1; None when only the router DB knows it), in
# insertion order and capped (R3): oldest marks drop first.
_LOCAL_THREADS: dict[str, str | None] = {}
LOCAL_THREADS_MAX = 1000
# R4-1: a late "Local model unavailable" 503 gets this fixed note, never the owner's text.
_LOCAL_FAILED_NOTE = "The local model could not answer. Nothing was sent anywhere else."


def _mark_local(thread_id: str, engine: str | None = None) -> None:
    # shortcut: an evicted unmapped thread loses its mark after 1000 newer local threads; the router DB still holds mapped ones
    _LOCAL_THREADS.pop(thread_id, None)
    _LOCAL_THREADS[thread_id] = engine
    while len(_LOCAL_THREADS) > LOCAL_THREADS_MAX:
        del _LOCAL_THREADS[next(iter(_LOCAL_THREADS))]


def _thread_local_state(thread_id: str) -> bool | None:
    """True when this thread is a local-only chat (seen here, or mapped to a local_only
    row in the router DB), False when the DB says it is not, None when the DB cannot
    answer (absent on a Mac-only deploy, unreadable, older schema). G3: callers treat
    None as local (fail closed), so the request skips every cloud fast path."""
    if thread_id in _LOCAL_THREADS:
        return True
    try:  # any failure (the Mac copy may have no cards._db at all) is "cannot tell", never a 500
        conn = cards._db()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT COALESCE(s.local_only, 0) AS l FROM thread_map t "
                               "JOIN sessions s ON s.id=t.session_id WHERE t.thread_id=?",
                               (thread_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return bool(row and row["l"])


def _thread_is_local(thread_id: str) -> bool:
    """True when this thread is (or may be) a local-only chat; "cannot tell" counts as local."""
    return _thread_local_state(thread_id) is not False


# R3-1: one threaded request at a time per thread, from the local decision through
# the router call, so a follow-up never overtakes message 1 (thread_id -> [lock, users]).
_THREAD_GATES: dict[str, list] = {}


async def _hey_in_thread_order(thread_id: str, call):
    gate = _THREAD_GATES.setdefault(thread_id, [asyncio.Lock(), 0])
    gate[1] += 1
    try:
        async with gate[0]:
            return await call()
    finally:
        gate[1] -= 1
        if not gate[1]:
            del _THREAD_GATES[thread_id]


async def _reply_is_local(data: dict) -> bool:
    """The router marks local rows with `upstream`; a local-only kind says it too."""
    kind = data.get("kind")
    return "upstream" in data or (isinstance(kind, str) and await central.is_local_engine(kind))


async def _mark_from_reply(thread_id: str, data: dict, engine: str | None,
                           local: bool, unsure: bool, may_clear: bool = True) -> None:
    """Keep the thread's local mark (and its engine) in step with the router's reply.

    A late reply runs outside the thread gate and may be older than a newer local
    request's mark, so `_deliver_late` passes may_clear=False: it only ever adds a mark."""
    kind = data.get("kind")
    kind_local = isinstance(kind, str) and await central.is_local_engine(kind)
    if may_clear and isinstance(kind, str) and not kind_local and "upstream" not in data:
        _LOCAL_THREADS.pop(thread_id, None)  # the router put it on a cloud engine: the early mark was wrong
    elif (local and not unsure) or kind_local or "upstream" in data:
        value = kind if kind_local else engine
        if not kind_local and engine in (None, "", "hub"):  # R5 sweep: a no-engine reply keeps the stored engine
            value = _LOCAL_THREADS.get(thread_id) or value
        _mark_local(thread_id, value)


def _is_local_unavailable(exc: BaseException | None) -> bool:
    """The router's own "Local model unavailable:" 503 (no row was made)."""
    reply = central._detail_reply(exc) if isinstance(exc, central.RouterError) else None
    return reply is not None and reply.status_code == 503


async def _router_reply(text: str, thread_id: str, msg_id: str,
                        engine: str | None = None, new_session: bool = False,
                        local: bool = False, unsure: bool = False) -> dict:
    """central.route (source "voice"), hard-bounded; failure → canned reply, never a 500.

    `reply` always holds something speakable: the router's reply, else its
    needs_input prompt, else the session title. action/session_id/title (and
    prompt/options on needs_input) are passed through for the phone.
    """
    task = asyncio.ensure_future(
        central.route(thread_id, msg_id, text, "voice", **central.route_opts(engine, new_session)))
    try:
        data = await asyncio.wait_for(asyncio.shield(task), timeout=ROUTER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # The router keeps going; hand its answer to the owner when it lands.
        late = asyncio.ensure_future(
            _deliver_late(task, text, thread_id, msg_id, engine, new_session, local, unsure))
        _late_tasks.add(late)
        late.add_done_callback(_late_tasks.discard)
        return {"reply": _TIMEOUT_REPLY}
    except central.RouterError as exc:
        passthrough = central._detail_reply(exc)  # C12: a local-model error in the router's own words
        if passthrough is not None:
            raise passthrough from None
        return {"reply": _ROUTER_DOWN_REPLY}
    except Exception:
        return {"reply": _ROUTER_DOWN_REPLY}
    await _mark_from_reply(thread_id, data, engine, local, unsure)
    out = {k: data[k] for k in ("action", "session_id", "kind", "title", "prompt", "options") if k in data}
    out["reply"] = _speakable(data)
    return out


def _speakable(data: dict) -> str:
    return str(data.get("reply") or data.get("prompt") or data.get("title") or "On it.")


# Late answers: strong refs so the tasks aren't garbage-collected mid-flight.
_late_tasks: set[asyncio.Future] = set()
# Retries reuse msg_id, so the router returns the stored answer instead of
# routing twice; stay well under its 600 s pending-row takeover.
LATE_ANSWER_MAX_SECONDS = 300
LATE_RETRY_SECONDS = 5


async def _deliver_late(task: asyncio.Future, text: str, thread_id: str, msg_id: str,
                        engine: str | None = None, new_session: bool = False,
                        local: bool = False, unsure: bool = False) -> None:
    """Wait out a router call that missed the request deadline, then send its answer to the app + WhatsApp.

    `unsure`: local only because the router DB cannot tell (R3): a neutral app notice, no WhatsApp, no mark."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LATE_ANSWER_MAX_SECONDS
    failure: BaseException | None = None
    try:
        data = await task
    except Exception as exc:  # the gateway's own read timeout fired; the router still finishes
        data, failure = None, exc
    # A local 503 is final (the router made no row): stop retrying and say so below.
    while data is None and not _is_local_unavailable(failure) and loop.time() < deadline:
        await asyncio.sleep(LATE_RETRY_SECONDS)
        try:
            data = await central.route(thread_id, msg_id, text, "voice",
                                       **central.route_opts(engine, new_session))
        except Exception as exc:
            data, failure = None, exc
    if data is None:
        if _is_local_unavailable(failure):  # R4-1: the owner was told "Still thinking"; fixed text only
            await notify.push_app("answer", "Local chat", _LOCAL_FAILED_NOTE, None)
            await notify.whatsapp_owner(_LOCAL_FAILED_NOTE, "[jarvis] Local chat")
        return
    sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    # G2: later follow-ups on this thread stay local — no question, no router title, no reply
    await _mark_from_reply(thread_id, data, engine, local, unsure, may_clear=False)
    if (local and not unsure) or await _reply_is_local(data):  # C12: content-free
        await notify.push_app("answer", "Local chat",
                              "The local model answered. Open the Jarvis app to read it.", sid)
        await notify.whatsapp_owner("The local model answered. Open the Jarvis app to read it.",
                                    "[jarvis] Local chat")
        return
    if local:  # "cannot tell": content-free, and the WhatsApp copy stays dropped (fail closed)
        await notify.push_app("answer", "Answer ready", "Your answer is ready. Open the Jarvis app.", sid)
        return
    question = " ".join(text.split())[:80]
    reply = _speakable(data)
    if data.get("action") == "answered":
        title, body = f"Answer: {question}", reply
    else:
        title, body = f"Started: {data.get('title') or question}", "Open the chat in the Jarvis app to follow it."
    await notify.push_app("answer", title, body, sid)
    await notify.whatsapp_owner(f"{title}\n\n{body}", f"[jarvis] {title}")


# The Shortcut sends the shared token; the iPhone app sends `ios_token`.
@router.post("/siri/hey", dependencies=[Depends(require_bearer_any("token", "ios_token"))])
async def siri_hey(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """`{"text": "<transcript>", "thread_id"?: str, "msg_id"?: str}` → local intent or router → `{"reply": ...}`.

    A client `msg_id` (central.ID_PATTERN) is passed to the router so a retry is
    idempotent; without one each call gets a fresh id.

    The body is read off the Request (not a Pydantic param) so the Shortcut's
    POST works without a Content-Type header. Beyond 2 in-flight or 20/60 s
    router calls the answer is an immediate busy reply — never a queue. Requests
    on one client thread do wait for each other (R3-1).
    """
    try:
        body = await request.json()
        text = str(body.get("text") or "")
        client_thread = body.get("thread_id")
        thread_id = str(client_thread or f"voice-{uuid.uuid4().hex}")
        msg_id = str(body.get("msg_id") or uuid.uuid4().hex)
        engine = body.get("engine")
        new_session = body.get("new_session") is True
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail='expected JSON body {"text": "..."}')
    if not client_thread:
        return await _hey(request, authorization, text, client_thread, thread_id, msg_id, engine, new_session)
    # R3-1: a request naming a non-cloud engine marks its thread before any await (a
    # wrong mark only fails closed); the thread gate keeps later requests behind it.
    # R5-1: it never replaces a stored engine; requests already queued read that one.
    if (isinstance(engine, str) and central._ENGINE_RE.fullmatch(engine)
            and engine not in central._CLOUD_ENGINES and thread_id not in _LOCAL_THREADS):
        _mark_local(thread_id, engine)
    return await _hey_in_thread_order(thread_id, lambda: _hey(
        request, authorization, text, client_thread, thread_id, msg_id, engine, new_session))


async def _hey(request: Request, authorization: str | None, text: str, client_thread,
               thread_id: str, msg_id: str, engine, new_session: bool) -> dict:
    # C13/C14: a local-only engine — no cloud seam (quick.ask, the shared-token
    # quick.ask answer) may ever touch its text, and only the phone app may use it.
    # S2: a follow-up on a local thread is local too, whatever engine the request names.
    # R4-1: one that names no engine goes out on the thread's local engine, so the router
    # answers locally, 503s or 409s, and never classifies it (message 1 may have made no row).
    stored = _LOCAL_THREADS.get(thread_id) if client_thread else None
    if (engine in (None, "", "hub") and stored and central._ENGINE_RE.fullmatch(stored)
            and await central.is_local_engine(stored)):
        engine = stored
    named_local = await central.is_local_engine(engine if isinstance(engine, str) else None)
    thread_state = _thread_local_state(thread_id) if client_thread else False
    local = named_local or thread_state is not False
    unsure = not named_local and thread_state is None  # R3: local only because the DB cannot tell
    # R6-1: inside the gate the latest well-formed local pick always becomes the stored
    # engine, before any early return (status phrase, 403, length 400).
    if client_thread and named_local and central._ENGINE_RE.fullmatch(engine):
        _mark_local(thread_id, engine)
    # R6-2: checked after that mark, so an over-long first local message still marks its thread.
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"text longer than {MAX_TEXT_CHARS} characters")
    if text.strip().lower() in _STATUS_PHRASES:
        return {"reply": _status_reply()}
    # Quick-ask prefixes: delegate to the terse /ask pipeline (never a brain slot).
    stripped = text.strip()
    quick_match = _QUICK_PREFIX_RE.match(stripped)
    if quick_match and not local:
        try:
            answer = (await quick.ask(stripped[quick_match.end():])).answer
        except Exception:
            return {"reply": "quick ask failed, try again"}
        return {"reply": answer or ""}
    # The phone (ios_token, not the shared token) vs. the Siri Shortcut
    # (shared token) — computed once, reused below for the router gate too.
    is_phone = not ios_token_is_shared() and _bearer_matches(
        authorization, config.get_token(name="ios_token")
    )
    # C13: the local model works only from the phone app, never the shared-token Shortcut.
    if local and not is_phone:
        raise HTTPException(status_code=403, detail="The local model works only from the Jarvis app.")
    if local and client_thread and not unsure:
        # G2: mark it now, before routing: a `?` follow-up sent while this message is
        # still in flight (no thread_map row, no reply yet) must not reach quick.ask
        # R5 sweep: a malformed id (fails closed as local, then 400s) never replaces the stored engine
        _mark_local(thread_id, engine if named_local and central._ENGINE_RE.fullmatch(engine)
                    else _LOCAL_THREADS.get(thread_id))
    # Screen intents (SC7): launch the control page instead of a brain call —
    # the same launch_screen POST /screen/launch uses, never an HTTP self-call.
    # Phone callers only match an exact button payload (see _PHONE_SCREEN_CMDS);
    # the Shortcut's free-form transcript still uses route()'s keyword matching.
    cmd = _PHONE_SCREEN_CMDS.get(stripped.lower()) if is_phone else voice_router.route(text)
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
    # The dependency already let the bearer in; only the phone (is_phone) goes on
    # to the router. Every other caller here is the shared-token Shortcut — it
    # never reaches central.route; a general question gets a real answer from
    # quick.ask instead.
    if not is_phone:
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
    if engine is not None and not (isinstance(engine, str) and central._ENGINE_RE.fullmatch(engine)):
        raise HTTPException(status_code=400, detail="invalid engine")
    if _brain_semaphore.locked() or not _brain_slot_available():
        return {"reply": _BUSY_REPLY}
    async with _brain_semaphore:
        return await _router_reply(text, thread_id, msg_id, engine, new_session, local=local, unsure=unsure)
