"""Central chat: an explicit allowlist proxy from the iPhone app to the mini PC router.

Contract: .orchestrator/inbox/fable2-central-chat-framing.md (router API §2 +
addendum §6). Every route here takes the Bearer `ios_token` only — never the
shared token — and maps onto exactly one router call; there is no generic
passthrough. The router bearer (`MINIPC_ROUTER_TOKEN`, vault `## Jarvis
Companion`) is read per request through `common.config.get_token` and is never
logged, echoed, or returned. Router failures surface as a 502 with a short
generic detail (never the upstream body).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path as FilePath

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from common import config
from gateway.auth import require_bearer_any

logger = logging.getLogger(__name__)

ROUTER_BASE_DEFAULT = "https://minipc.<tailnet>.<tld>:8801"
ROUTER_TOKEN_NAME = "MINIPC_ROUTER_TOKEN"

# Plain JSON calls: a /route may spawn a session, so allow it a little longer.
JSON_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# SSE: no data for 10 min ends the stream; the phone resumes with Last-Event-ID.
SSE_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=10.0, pool=5.0)

ID_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
_ID_RE = re.compile(ID_PATTERN)
_SEQ_RE = re.compile(r"^\d{1,18}$")

ROUTER_UNAVAILABLE = "router unavailable"
ROUTER_UNCONFIGURED = "router not configured"

# Keys passed back to the phone; anything else the router adds is dropped.
_ROUTE_KEYS = ("action", "session_id", "kind", "title", "reply", "prompt", "options")
_SESSION_KEYS = (
    "session_id", "kind", "title", "state", "cwd", "updated_at",
    "project", "task", "summary", "phase",
)
_PHASE_FALLBACK = {"running": "in_progress", "needs_input": "in_review"}

router = APIRouter(dependencies=[Depends(require_bearer_any("ios_token"))])
# The Agency Floor page holds no data or secrets (it fetches everything with the
# bearer the app injects), so it is the one /central route served without auth.
public_router = APIRouter()

FLOOR_HTML = FilePath(__file__).resolve().parent / "static" / "floor.html"
FLOOR_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

AGENTIC_OS_URL_DEFAULT = "http://127.0.0.1:8787"
AGENTIC_OS_TOKEN_PATH = FilePath.home() / ".config" / "agentic-os" / "token"
AGENTIC_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_HUB_KEYS = ("key", "dirLabel", "engine", "active", "lastTs")
_AGENT_KEYS = ("id", "persona", "engine", "label", "active", "status")


class RouterError(Exception):
    """Router unreachable, unconfigured, or answered non-2xx. str() is safe to return."""


def router_base() -> str:
    return os.environ.get("MINIPC_ROUTER_URL", ROUTER_BASE_DEFAULT).rstrip("/")


def router_token() -> str | None:
    """The router bearer, read from the vault per call (tests stub this)."""
    return config.get_token(name=ROUTER_TOKEN_NAME)


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """The outbound router client (tests swap in an ASGI transport here)."""
    return httpx.AsyncClient(timeout=timeout)


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    token = router_token()
    if not token:
        raise RouterError(ROUTER_UNCONFIGURED)
    headers = {"Authorization": f"Bearer {token}"}
    if extra:
        headers.update(extra)
    return headers


async def _router_json(method: str, path: str, body: dict | None = None):
    """One JSON call to the router; any failure -> RouterError (no upstream text)."""
    headers = _headers()
    try:
        async with _client(JSON_TIMEOUT) as client:
            resp = await client.request(method, f"{router_base()}{path}", json=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("router %s %s failed: %s", method, path, type(exc).__name__)
        raise RouterError(ROUTER_UNAVAILABLE) from None
    if resp.status_code // 100 != 2:
        logger.warning("router %s %s -> %d", method, path, resp.status_code)
        raise RouterError(f"router error {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise RouterError("router sent invalid json") from None


def _pick(data, keys: tuple[str, ...]) -> dict:
    return {k: data[k] for k in keys if isinstance(data, dict) and k in data}


async def route(thread_id: str, msg_id: str, text: str, source: str) -> dict:
    """POST /route -> the allowlisted subset of the router's answer."""
    data = await _router_json(
        "POST", "/route",
        {"thread_id": thread_id, "msg_id": msg_id, "text": text, "source": source},
    )
    return _pick(data, _ROUTE_KEYS)


def _bad_gateway(exc: RouterError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def _session(item: dict) -> dict:
    out = _pick(item, _SESSION_KEYS)
    if out.get("phase") not in ("in_progress", "in_review", "done"):
        out["phase"] = _PHASE_FALLBACK.get(out.get("state"), "done")
    return out


class ChatBody(BaseModel):
    thread_id: str = Field(pattern=ID_PATTERN)
    msg_id: str = Field(pattern=ID_PATTERN)
    text: str = Field(min_length=1, max_length=4000)


class MessageBody(BaseModel):
    msg_id: str = Field(pattern=ID_PATTERN)
    text: str = Field(min_length=1, max_length=4000)


@router.post("/chat")
async def chat(body: ChatBody) -> dict:
    try:
        return await route(body.thread_id, body.msg_id, body.text, "app")
    except RouterError as exc:
        raise _bad_gateway(exc)


@router.get("/central/sessions")
async def sessions() -> list[dict]:
    try:
        data = await _router_json("GET", "/sessions")
    except RouterError as exc:
        raise _bad_gateway(exc)
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="router sent invalid json")
    return [_session(item) for item in data if isinstance(item, dict)]


@router.post("/central/sessions/{session_id}/message")
async def session_message(body: MessageBody, session_id: str = Path(pattern=ID_PATTERN)) -> dict:
    try:
        data = await _router_json(
            "POST", f"/sessions/{session_id}/message", {"msg_id": body.msg_id, "text": body.text}
        )
    except RouterError as exc:
        raise _bad_gateway(exc)
    return _pick(data, ("ok", "action"))


@router.post("/central/sessions/{session_id}/stop")
async def session_stop(session_id: str = Path(pattern=ID_PATTERN)) -> dict:
    try:
        data = await _router_json("POST", f"/sessions/{session_id}/stop")
    except RouterError as exc:
        raise _bad_gateway(exc)
    return _pick(data, ("ok",))


async def _sse(path: str, last_event_id: str | None) -> StreamingResponse:
    """Open the upstream SSE stream, 502 before any byte on failure, then relay raw bytes."""
    extra = {"Accept": "text/event-stream"}
    if last_event_id and _SEQ_RE.fullmatch(last_event_id.strip()):
        extra["Last-Event-ID"] = last_event_id.strip()
    try:
        headers = _headers(extra)
    except RouterError as exc:
        raise _bad_gateway(exc)
    client = _client(SSE_TIMEOUT)
    try:
        resp = await client.send(client.build_request("GET", f"{router_base()}{path}", headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("router GET %s failed: %s", path, type(exc).__name__)
        raise HTTPException(status_code=502, detail=ROUTER_UNAVAILABLE)
    if resp.status_code != 200:
        status = resp.status_code
        await resp.aclose()
        await client.aclose()
        logger.warning("router GET %s -> %d", path, status)
        raise HTTPException(status_code=502, detail=f"router error {status}")

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            # Mid-stream drop: just end; the phone reconnects with Last-Event-ID.
            logger.info("router GET %s stream ended: %s", path, type(exc).__name__)
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/central/sessions/{session_id}/events")
async def session_events(
    session_id: str = Path(pattern=ID_PATTERN),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    return await _sse(f"/sessions/{session_id}/events", last_event_id)


@router.get("/central/feed")
async def feed(last_event_id: str | None = Header(default=None)) -> StreamingResponse:
    return await _sse("/feed", last_event_id)


@public_router.get("/central/floor")
async def floor() -> HTMLResponse:
    return HTMLResponse(
        FLOOR_HTML.read_text(),
        headers={"Cache-Control": "no-store", "Content-Security-Policy": FLOOR_CSP},
    )


def agentic_os_base() -> str:
    return os.environ.get("AGENTIC_OS_URL", AGENTIC_OS_URL_DEFAULT).rstrip("/")


def agentic_os_token() -> str | None:
    """The agentic-os dashboard bearer, read per request (tests stub this)."""
    try:
        return AGENTIC_OS_TOKEN_PATH.read_text().strip() or None
    except OSError:
        return None


def _agentic_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """The outbound agentic-os client (tests swap in an ASGI transport here)."""
    return httpx.AsyncClient(timeout=timeout)


def _hub(item: dict) -> dict:
    out = _pick(item, _HUB_KEYS)
    agents = item.get("agents")
    out["agents"] = [_pick(a, _AGENT_KEYS) for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []
    return out


@router.get("/central/agents")
async def agents() -> dict:
    """The Mac's hub sessions + their agents from agentic-os, trimmed; down -> no hubs."""
    empty = {"device": None, "hubs": []}
    token = agentic_os_token()
    if not token:
        return empty
    try:
        async with _agentic_client(AGENTIC_TIMEOUT) as client:
            resp = await client.get(
                f"{agentic_os_base()}/api/snapshot", headers={"Authorization": f"Bearer {token}"}
            )
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("agentic-os snapshot failed: %s", type(exc).__name__)
        return empty
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return empty
    hubs = sessions.get("hubs")
    device = sessions.get("device")
    return {
        "device": device if isinstance(device, str) else None,
        "hubs": [_hub(h) for h in hubs if isinstance(h, dict)] if isinstance(hubs, list) else [],
    }
