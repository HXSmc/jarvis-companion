"""Owner notes: tell the owner something happened while they weren't looking.

Two channels:
- the iPhone app, as an ``owner_note`` push over the ios WebSocket (the app
  shows it on Home and raises a local notification), and
- WhatsApp to the owner's own number (jarvis-phone ``config/owner.json``).

Two producers:
- siri.py: a home-screen question whose router answer lands after the
  request already returned ("late answer") -> app + WhatsApp.
- crossroads_watcher(): an owner-started router session that stops and waits
  for the owner (blocked on a question, or its turn ended) -> app, and
  WhatsApp only when the app isn't connected.

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
import uuid

import httpx

from gateway import central, inbox, registry

logger = logging.getLogger(__name__)

WHATSAPP_MCP_URL = os.environ.get("WHATSAPP_MCP_URL", "http://127.0.0.1:8765/mcp")
WHATSAPP_TIMEOUT = httpx.Timeout(20.0, connect=3.0)
# WhatsApp bodies are capped; the app gets the full text.
WHATSAPP_MAX_CHARS = 1500


async def push_app(kind: str, title: str, text: str, session_id: str | None = None) -> bool:
    """`owner_note` to the connected iPhone app; False when it isn't connected."""
    payload = {"kind": kind, "title": title, "text": text}
    if session_id:
        payload["session_id"] = session_id
    try:
        return await registry.send_to_role(
            "ios", {"id": f"note-{uuid.uuid4().hex[:8]}", "type": "owner_note", "payload": payload}
        )
    except Exception:
        return False


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
# Router session states (public /sessions): "waiting" health = blocked on a
# question/permission; running -> idle = the turn ended and it waits on the owner.
_LIVE = {"running"}


def _owner_session(s: dict) -> bool:
    """Started by the owner here: not a hook-registered/Mac/child session."""
    return (
        s.get("spawned_by") != "unknown"
        and not s.get("parent_session_id")
        and not str(s.get("device") or "").lower().startswith("mac")  # every box row says "Mini PC"
        and not s.get("throwaway")
    )


def crossroads(prev: dict[str, tuple], sessions: list[dict]) -> list[tuple[str, str, str, str]]:
    """Pure transition check -> [(kind, title, text, session_id)]; updates `prev`.

    First sight of a session only records it (no burst on gateway restart).
    """
    out = []
    for s in sessions:
        sid = s.get("session_id")
        if not isinstance(sid, str) or not _owner_session(s):
            continue
        cur = (s.get("state"), s.get("health"))
        before = prev.get(sid)
        prev[sid] = cur
        if before is None:
            continue
        title = str(s.get("title") or "Jarvis session")
        summary = str(s.get("summary") or "").strip()
        if cur[1] == "waiting" and before[1] != "waiting":
            out.append(("crossroads", f"Needs your decision: {title}", summary or "It is waiting for your answer.", sid))
        elif cur[0] == "idle" and before[0] in _LIVE:
            out.append(("turn", f"Waiting for you: {title}", summary or "It finished its turn.", sid))
    return out


async def crossroads_watcher() -> None:
    """Box-only poller of the router's sessions; never raises out."""
    prev: dict[str, tuple] = {}
    while True:
        try:
            data = await central._router_json("GET", "/sessions")
            if isinstance(data, list):
                for kind, title, text, sid in crossroads(prev, [s for s in data if isinstance(s, dict)]):
                    delivered = await push_app(kind, title, text, sid)
                    if not delivered:
                        await whatsapp_owner(f"{title}\n\n{text}\n\nOpen the Jarvis app to reply.", f"[jarvis] {title}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed poll never kills the watcher
            logger.info("notify: crossroads poll failed: %s", type(exc).__name__)
        await asyncio.sleep(CROSSROADS_POLL_SECONDS)
