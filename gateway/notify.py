"""Owner notes: tell the owner something happened while they weren't looking.

Two channels:
- the iPhone app, as an ``owner_note`` push over the ios WebSocket (the app
  shows it on Home and raises a local notification), and
- WhatsApp to the owner's own number (jarvis-phone ``config/owner.json``).

Two producers:
- siri.py: a home-screen question whose router answer lands after the
  request already returned ("late answer") -> app + WhatsApp.
- crossroads_watcher(): a router session that needs the owner (blocked on
  a question, stuck, failed) -> app, and WhatsApp only when the app doesn't
  confirm the note (not connected, or asleep behind a stale socket); a hook-registered session (Mac, box terminal) that finished a
  turn -> app only. (A fully finished router task is inbox.task_done's job:
  WhatsApp via the router ledger, plus an app note.)

Every app note also lands in a short in-memory list the app catches up from
(GET /notify/notes?after=<seq>) on foreground and background refresh, so a
note sent while the phone was offline or asleep is not lost. No APNs.

WhatsApp: on the box (MAC_GATEWAY_URL set) via the box's own mcp-whatsapp
daemon over MCP HTTP (the box never writes jarvis-phone queue files); on the
Mac via the jarvis-phone outbound queue, like inbox.task_done. Under pytest
inbox._phone_dir() is None, so no test can ever send a real message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque

import httpx
from fastapi import APIRouter, Depends, Query

from gateway import agents, cards, central, inbox, registry
from gateway.auth import require_bearer_any

logger = logging.getLogger(__name__)

WHATSAPP_MCP_URL = os.environ.get("WHATSAPP_MCP_URL", "http://127.0.0.1:8765/mcp")
WHATSAPP_TIMEOUT = httpx.Timeout(20.0, connect=3.0)
# WhatsApp bodies are capped; the app gets the full text.
WHATSAPP_MAX_CHARS = 1500


# A socket can look connected while iOS has the app asleep: a note only counts as
# delivered when the app confirms it (it answers every owner_note with a result).
NOTE_ACK_S = 8.0
NOTES_KEEP = 50
_notes: deque[dict] = deque(maxlen=NOTES_KEEP)
_last_seq = 0

router = APIRouter(dependencies=[Depends(require_bearer_any("ios_token"))])


def _next_seq() -> int:
    """Epoch ms, strictly rising: a restarted gateway never reuses a seq the app saw."""
    global _last_seq
    _last_seq = max(_last_seq + 1, time.time_ns() // 1_000_000)
    return _last_seq


@router.get("/notify/notes")
async def notes(after: int = Query(default=0, ge=0)) -> list[dict]:
    """App notes newer than `after` (the app's last seen seq), oldest first."""
    return [n for n in _notes if n["seq"] > after]


async def push_app(kind: str, title: str, text: str, session_id: str | None = None,
                   confirm: bool = False) -> bool:
    """`owner_note` to the connected iPhone app; False when it isn't connected,
    or (`confirm`) when the app does not confirm it within NOTE_ACK_S.

    Kept for GET /notify/notes either way; `seq` lets the app drop a note it
    already showed.
    """
    payload = {"kind": kind, "title": title, "text": text, "seq": _next_seq()}
    if session_id:
        payload["session_id"] = session_id
    _notes.append(payload)
    note_id = f"note-{uuid.uuid4().hex[:8]}"
    ack = asyncio.get_running_loop().create_future() if confirm else None
    if ack:
        agents._pending_replies[note_id] = ack
    try:
        if not await registry.send_to_role("ios", {"id": note_id, "type": "owner_note", "payload": payload}):
            return False
        if ack:
            await asyncio.wait_for(ack, NOTE_ACK_S)
        return True
    except asyncio.TimeoutError:
        logger.info("notify: the app did not confirm a %s note (asleep?)", kind)
        return False
    except Exception:
        return False
    finally:
        if ack:
            agents._pending_replies.pop(note_id, None)


def _mcp_result(resp: httpx.Response) -> dict | None:
    """JSON-RPC body from a plain JSON or SSE (`data: {...}`) MCP response."""
    text = resp.text
    if "data:" in text:
        lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = lines[-1] if lines else ""
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        return None


async def _whatsapp_mcp_send(to: str, text: str) -> bool:
    """initialize -> tools/call send_message on the box's mcp-whatsapp daemon."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "jarvis-gateway", "version": "1"}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "send_message", "arguments": {"recipient": to, "message": text}},
    }
    try:
        async with httpx.AsyncClient(timeout=WHATSAPP_TIMEOUT) as client:
            resp = await client.post(WHATSAPP_MCP_URL, json=init, headers=headers)
            session = resp.headers.get("Mcp-Session-Id")
            if session:
                headers["Mcp-Session-Id"] = session
            await client.post(WHATSAPP_MCP_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
            resp = await client.post(WHATSAPP_MCP_URL, json=call, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("notify: whatsapp daemon unreachable: %s", type(exc).__name__)
        return False
    result = (_mcp_result(resp) or {}).get("result") or {}
    try:  # send_message answers {"Success", "Message", "ID"} as its text content
        ok = resp.status_code == 200 and not result.get("isError") and json.loads(result["content"][0]["text"]).get("Success") is True
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        ok = False
    if not ok:
        logger.warning("notify: whatsapp send failed (HTTP %d)", resp.status_code)
    return ok


# ntfy: the owner's self-hosted push server. It rings the phone like any app, unlike a
# WhatsApp from the owner's own number (never notifies) or the app (frozen in the
# background). Off unless JARVIS_NTFY_URL and JARVIS_NTFY_TOPIC are set (.env);
# JARVIS_NTFY_TOKEN when the topic needs auth.
NTFY_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def _ntfy_target() -> tuple[str, str] | None:
    url, topic = os.environ.get("JARVIS_NTFY_URL", "").rstrip("/"), os.environ.get("JARVIS_NTFY_TOPIC", "")
    if not (url and topic) or os.environ.get("PYTEST_CURRENT_TEST"):  # a test never pushes for real
        return None
    return url, topic


async def push_ntfy(kind: str, title: str, text: str) -> bool:
    """One ntfy push (JSON publish: UTF-8 safe); False = not configured or not sent."""
    target = _ntfy_target()
    if target is None:
        return False
    url, topic = target
    headers = {"Authorization": f"Bearer {os.environ['JARVIS_NTFY_TOKEN']}"} if os.environ.get("JARVIS_NTFY_TOKEN") else {}
    body = {"topic": topic, "title": title, "message": text or title,
            "tags": ["white_check_mark" if kind == "done" else "bell"],
            "priority": 3 if kind == "done" else 4}
    try:
        async with httpx.AsyncClient(timeout=NTFY_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("notify: ntfy unreachable: %s", type(exc).__name__)
        return False
    if resp.status_code >= 300:
        logger.warning("notify: ntfy refused the push (HTTP %d)", resp.status_code)
        return False
    return True


async def ring(kind: str, title: str, text: str) -> bool:
    """The out-of-app ping: ntfy when it is set up, else WhatsApp (not for turn-ends)."""
    if _ntfy_target() is not None:
        return await push_ntfy(kind, title, text)
    if kind == "done":
        return False
    return await whatsapp_owner(f"{title}\n\n{text}\n\nOpen the Jarvis app to reply.", f"[jarvis] {title}")


async def whatsapp_owner(text: str, subject: str) -> bool:
    """One WhatsApp to the owner's own number; False = not sent (never raises)."""
    text = text if len(text) <= WHATSAPP_MAX_CHARS else text[: WHATSAPP_MAX_CHARS - 1] + "…"
    directory = inbox._phone_dir()
    if directory is None:
        return False
    if not central._mac_gateway():
        return inbox._enqueue_owner_text(text, subject)
    phone, _ = inbox._owner_contact(directory)
    if not phone:
        logger.warning("notify: no owner phone_whatsapp in owner.json, message skipped")
        return False
    return await _whatsapp_mcp_send(phone.lstrip("+"), text)


# --- crossroads ---------------------------------------------------------------

CROSSROADS_POLL_SECONDS = 15
# Router public /sessions: health "waiting" = blocked on a question/permission,
# "attention" = stuck (DONE rejected by the judge, watchdog), "failed".
# A hook-registered session's turn end only counts after this long running,
# so a quick back-and-forth at the keyboard never pings the phone.
DONE_MIN_RUN_SECONDS = 60


def _owner_session(s: dict) -> bool:
    """Started by the owner here: not a hook-registered/Mac/child session."""
    return (
        s.get("spawned_by") != "unknown"
        and not s.get("parent_session_id")
        and not str(s.get("device") or "").lower().startswith("mac")  # every box row says "Mini PC"
        and not s.get("throwaway")
    )


def crossroads(prev: dict[str, tuple], sessions: list[dict], now: float | None = None) -> list[tuple[str, str, str, str]]:
    """Pure transition check -> [(kind, title, text, session_id)]; updates `prev`.

    Router sessions (_owner_session): health turning waiting, attention or
    failed. Their done is inbox.task_done's, except engine glm, which the
    router never reports there. Hook-registered sessions (Mac, box terminal):
    running -> idle (the Stop hook) after DONE_MIN_RUN_SECONDS; their
    "waiting" is a failed tool, not an ask, so it is skipped. Child and
    throwaway sessions never notify. First sight of a session only records it
    (no burst on gateway restart); one note per transition.
    """
    now = time.monotonic() if now is None else now
    out = []
    for s in sessions:
        sid = s.get("session_id")
        if not isinstance(sid, str) or s.get("parent_session_id") or s.get("throwaway"):
            continue
        state, health = s.get("state"), s.get("health")
        before = prev.get(sid)
        if state != "running":
            since = None
        elif before and before[0] == "running" and before[2] is not None:
            since = before[2]
        else:
            since = now
        prev[sid] = (state, health, since)
        if before is None:
            continue
        title = str(s.get("title") or "Jarvis session")
        summary = str(s.get("summary") or "").strip()
        if _owner_session(s):
            if health == "waiting" and before[1] != "waiting":
                ask = str(s.get("needs_prompt") or "").strip()  # the router's own pending question
                out.append(("crossroads", f"Needs your decision: {title}", ask or summary or "It is waiting for your answer.", sid))
            elif health == "attention" and before[1] != "attention":
                reason = str(s.get("attention_reason") or "").strip()
                out.append(("attention", f"Needs attention: {title}", reason or summary or "It looks stuck.", sid))
            elif health == "failed" and before[1] != "failed":
                out.append(("failed", f"Failed: {title}", summary or "It stopped with an error.", sid))
            elif state == "done" and before[0] != "done" and s.get("engine") == "glm":
                out.append(("done", f"Done: {title}", summary or "Finished. Open the chat to see the result.", sid))
        elif before[0] == "running" and state == "idle" and before[2] is not None and now - before[2] >= DONE_MIN_RUN_SECONDS:
            device = str(s.get("device") or "")
            label = f"Done on {device}" if device else "Done"
            out.append(("done", f"{label}: {title}", summary or "It finished and is waiting for you.", sid))
    return out


# --- card asks (gateway/cards.py) ------------------------------------------------

_card_needs_seen: deque[tuple] = deque(maxlen=200)
_titles: dict[str, str] = {}  # session id -> title, from the watcher's polls
_deliveries: set[asyncio.Task] = set()  # strong refs: asyncio keeps only weak ones


async def deliver(kind: str, title: str, text: str, sid: str | None) -> None:
    """A note the owner must not miss: the app, else ntfy/WhatsApp when the app does not confirm it."""
    if not await push_app(kind, title, text, sid, confirm=True):
        await ring(kind, title, text)


def spawn_deliver(kind: str, title: str, text: str, sid: str | None) -> None:
    """deliver() in the background: a request path must not wait for the app's confirmation."""
    task = asyncio.get_running_loop().create_task(deliver(kind, title, text, sid))
    _deliveries.add(task)
    task.add_done_callback(_deliveries.discard)


def on_card_need(session_id: str, need: dict) -> None:
    """cards.NEED_LISTENERS callback: one note per new ask on a session's card."""
    key = (session_id, need.get("id"))
    if key in _card_needs_seen:
        return
    _card_needs_seen.append(key)
    title = f"Needs you: {_titles.get(session_id) or 'a Jarvis session'}"
    text = str(need.get("question") or "").strip() or "It is waiting for your answer."
    spawn_deliver("card", title, text, session_id)


def _card_need_open(sid: str) -> bool:
    """The session's card has an unanswered ask: on_card_need already told the owner."""
    try:
        need = (cards.load().get(sid) or {}).get("need")
    except Exception:
        return False
    return isinstance(need, dict) and not need.get("answered")


cards.NEED_LISTENERS.append(on_card_need)


async def crossroads_watcher() -> None:
    """Box-only poller of the router's sessions; never raises out."""
    prev: dict[str, tuple] = {}
    while True:
        try:
            data = await central._router_json("GET", "/sessions")
            if isinstance(data, list):
                rows = [s for s in data if isinstance(s, dict)]
                _titles.update({s["session_id"]: str(s["title"]) for s in rows
                                if isinstance(s.get("session_id"), str) and s.get("title")})
                for kind, title, text, sid in crossroads(prev, rows):
                    if kind == "crossroads" and _card_need_open(sid):
                        continue  # the card's own ask was the note
                    await deliver(kind, title, text, sid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed poll never kills the watcher
            logger.info("notify: crossroads poll failed: %s", type(exc).__name__)
        await asyncio.sleep(CROSSROADS_POLL_SECONDS)
