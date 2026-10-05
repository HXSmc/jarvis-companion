"""Remote screen control router: cookie handshake, JPEG frames, input, open-app.

Design: docs/superpowers/specs/2026-10-04-remote-screen-control-design.md
("gateway/screen.py" section). `/screen/*` accepts a `screen_session` cookie or
the Bearer `ios_token` — never the shared token (403, distinct from 401, so a
leaked windows-agent token cannot watch or drive either screen). Mac
frames/input run in-process via `mac.screen.driver` (imported lazily at call
time so the router mounts even before SC1 lands); windows frames/input ride
`gateway.agents.agent_roundtrip`. Sessions/nonces live in module dicts, swept
on access (max 5 live sessions, 12 h; nonces 60 s single-use). Nothing here
ever logs a token, cookie, nonce, or typed-text value.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from common import config
from gateway.agents import WINDOWS_ROLE, agent_roundtrip
from gateway.registry import roles, send_to_role
from mac.voice import router as voice_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/screen")

Target = Literal["mac", "windows"]

SCREEN_HTML = Path(__file__).resolve().parent / "static" / "screen.html"

NOT_AUTHENTICATED = "not authenticated"
SHARED_TOKEN_DETAIL = "shared token not valid for screen"
PC_OFFLINE_DETAIL = "pc offline"

SESSION_COOKIE = "screen_session"
SESSION_MAX = 5
SESSION_TTL_S = 43200  # 12 h — matches the cookie Max-Age.

BOOTSTRAP_TTL_S = 60.0

_INPUT_MAX_EVENTS = 60
_INPUT_WINDOW_S = 2.0

# token/nonce -> unix expiry; swept on access. Restart the gateway to reset.
_SESSIONS: dict[str, float] = {}
_BOOTSTRAPS: dict[str, float] = {}
# target -> monotonic timestamps of recent input events (rolling 60/2 s limit).
_input_times: dict[str, collections.deque[float]] = {}
# One lock per target so concurrent frame pulls don't interleave captures.
_frame_locks: dict[str, asyncio.Lock] = {"mac": asyncio.Lock(), "windows": asyncio.Lock()}


def _sweep(store: dict[str, float]) -> None:
    now = time.time()
    for key in [k for k, exp in store.items() if exp <= now]:
        del store[key]


def _mint_session() -> str:
    """Sweep expired sessions, evict the oldest past 5, mint a fresh token."""
    _sweep(_SESSIONS)
    while len(_SESSIONS) >= SESSION_MAX:
        oldest = min(_SESSIONS, key=_SESSIONS.get)
        del _SESSIONS[oldest]
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + SESSION_TTL_S
    return token


def _authorized(request: Request) -> str | None:
    """How the request authenticated: "cookie", "bearer", "shared", or None.

    "shared" means the Bearer matches the shared token — callers 403 it, the
    one screen-route scope rule with no exception in this module.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        _sweep(_SESSIONS)
        expiry = _SESSIONS.get(cookie)
        if expiry is not None and expiry > time.time():
            return "cookie"
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        value = header[7:]
        shared = config.get_token() or ""
        if shared and hmac.compare_digest(value.encode(), shared.encode()):
            return "shared"
        ios = config.get_token(name="ios_token") or ""
        if ios and hmac.compare_digest(value.encode(), ios.encode()):
            return "bearer"
    return None


def _require(request: Request) -> str:
    kind = _authorized(request)
    if kind == "shared":
        raise HTTPException(status_code=403, detail=SHARED_TOKEN_DETAIL)
    if kind is None:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED)
    return kind


def _windows_online() -> None:
    if WINDOWS_ROLE not in roles():
        raise HTTPException(status_code=503, detail=PC_OFFLINE_DETAIL)


def _input_slot_available(target: str, count: int) -> bool:
    """Consume `count` rolling-window slots; False = rate limited (60 events / 2 s)."""
    window = _input_times.setdefault(target, collections.deque())
    now = time.monotonic()
    while window and window[0] <= now - _INPUT_WINDOW_S:
        window.popleft()
    if len(window) >= _INPUT_MAX_EVENTS:
        return False
    for _ in range(count):
        window.append(now)
    return True


_Key = Literal[
    "return", "esc", "delete", "tab", "space", "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
]


class ScreenEvent(BaseModel):
    """One normalized input event (coords 0..1, scroll steps -1..1)."""

    type: Literal["move", "tap", "dblclick", "right", "drag", "scroll", "text", "key"]
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    dx: float | None = Field(default=None, ge=-1, le=1)
    dy: float | None = Field(default=None, ge=-1, le=1)
    button: Literal["left", "right"] = "left"
    text: str | None = Field(default=None, max_length=100)
    key: _Key | None = None
    mods: list[Literal["cmd", "ctrl", "alt", "shift"]] = []

    @model_validator(mode="after")
    def _fields_for_type(self) -> "ScreenEvent":
        if self.type in ("move", "tap", "dblclick", "right", "drag"):
            if self.x is None or self.y is None:
                raise ValueError(f"{self.type} event requires x and y")
        elif self.type == "scroll":
            if self.dx is None and self.dy is None:
                raise ValueError("scroll event requires dx or dy")
        elif self.type == "text":
            if not self.text:
                raise ValueError("text event requires text")
        elif self.type == "key" and self.key is None:
            raise ValueError("key event requires key")
        return self


class InputBody(BaseModel):
    events: list[ScreenEvent] = Field(min_length=1, max_length=20)


class OpenBody(BaseModel):
    name: str = Field(pattern=r"^[\w .+-]{1,64}$")


@router.get("")
async def screen_page(request: Request, boot: str | None = Query(default=None)) -> Response:
    """Serve the control page; a Bearer/valid-nonce handshake mints the session cookie.

    `boot` is a single-use 60 s nonce (minted by SC7's launch route): valid +
    unexpired -> consumed here and exchanged for a session cookie; bogus,
    expired, or reused -> 401 unless the request already carries a valid
    session cookie (page reload of a once-used boot link stays 200). The boot
    value is never logged.
    """
    kind = None
    if boot is not None:
        _sweep(_BOOTSTRAPS)
        expiry = _BOOTSTRAPS.pop(boot, None)
        if expiry is not None and expiry > time.time():
            kind = "boot"
    if kind is None:
        kind = _require(request)
    response = FileResponse(SCREEN_HTML)
    if kind in ("bearer", "boot"):
        response.set_cookie(
            SESSION_COOKIE, _mint_session(),
            max_age=SESSION_TTL_S, httponly=True, secure=True,
            samesite="strict", path="/screen",
        )
    return response


class LaunchBody(BaseModel):
    target: Target


# Tailscale serve base URL, computed once on first use (SCREEN_PUBLIC_BASE wins
# per call) — restart the gateway to re-derive after a tailnet rename.
_public_base_cache: str | None = None


def _tailscale_dns_name() -> str:
    """Self.DNSName off `tailscale status --json` — the derivation serve.sh prints."""
    out = subprocess.run(
        ["tailscale", "status", "--json"], capture_output=True, check=True
    ).stdout
    return json.loads(out)["Self"]["DNSName"].rstrip(".")


async def _public_base() -> str:
    env_base = os.environ.get("SCREEN_PUBLIC_BASE")
    if env_base:
        return env_base.rstrip("/")
    global _public_base_cache
    if _public_base_cache is None:
        _public_base_cache = f"https://{await run_in_threadpool(_tailscale_dns_name)}:8790"
    return _public_base_cache


async def launch_screen(target: Target) -> dict:
    """Mint a single-use 60 s boot nonce and push a navigate to the ios app (SC7).

    Returns {"url": ..., "pushed": bool} — pushed is False when no ios WS is
    connected (caller opens the url itself). Never sets a session cookie and
    never logs the nonce.
    """
    _sweep(_BOOTSTRAPS)
    nonce = secrets.token_urlsafe(24)
    _BOOTSTRAPS[nonce] = time.time() + BOOTSTRAP_TTL_S
    url = f"{await _public_base()}/screen?boot={nonce}&target={target}"
    pushed = False
    if "ios" in roles():
        # Same navigate shape handle_handoff_open pushes (flight.py:155).
        pushed = await send_to_role(
            "ios", {"id": uuid.uuid4().hex, "type": "navigate", "payload": {"url": url}}
        )
    return {"url": url, "pushed": pushed}


@router.post("/launch")
async def screen_launch(request: Request, body: LaunchBody) -> dict:
    # Launch-only exception to the shared-token 403: any valid auth (cookie,
    # ios Bearer, or the shared Bearer) may mint a boot nonce — never a session.
    if _authorized(request) is None:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED)
    return await launch_screen(body.target)


@router.get("/{target}/status")
async def screen_status(request: Request, target: Target) -> dict:
    _require(request)
    if target == "mac":
        from mac.screen import driver
        return {
            "target": "mac", "online": True,
            "tcc_ok": driver.tcc_ok(), "display_asleep": driver.display_asleep(),
        }
    return {"target": "windows", "online": WINDOWS_ROLE in roles(), "tcc_ok": None, "display_asleep": None}


@router.get("/{target}/frame")
async def screen_frame(
    request: Request,
    target: Target,
    w: int = Query(default=1170, ge=320, le=1600),
    q: int = Query(default=55, ge=20, le=90),
) -> Response:
    _require(request)
    async with _frame_locks[target]:
        if target == "mac":
            from mac.screen import driver
            jpeg, _, _ = await run_in_threadpool(driver.capture_jpeg, w, q)
        else:
            _windows_online()
            reply = await agent_roundtrip(voice_router.request_envelope("screen_frame", {"w": w, "q": q}))
            if not reply.get("ok"):
                raise HTTPException(status_code=503, detail=str(reply.get("data") or PC_OFFLINE_DETAIL))
            jpeg = base64.b64decode(reply["data"]["jpeg"])
    etag = hashlib.sha1(jpeg).hexdigest()
    if request.headers.get("if-none-match", "").strip().strip('"') == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content=jpeg, media_type="image/jpeg", headers={"ETag": etag})


@router.post("/{target}/input")
async def screen_input(request: Request, target: Target, body: InputBody) -> dict:
    _require(request)
    if not _input_slot_available(target, len(body.events)):
        raise HTTPException(status_code=429, detail="too many input events")
    events = [event.model_dump() for event in body.events]
    # Audit trail: target, count, event types only — never coordinates or text.
    logger.info("screen input: %s %d events %s", target, len(events), [e["type"] for e in events])
    if target == "mac":
        from mac.screen import driver
        await run_in_threadpool(driver.send_events, events)
    else:
        _windows_online()
        reply = await agent_roundtrip(voice_router.request_envelope("screen_input", {"events": events}))
        if not reply.get("ok"):
            raise HTTPException(status_code=503, detail=str(reply.get("data") or PC_OFFLINE_DETAIL))
    return {"ok": True}


@router.post("/{target}/open")
async def screen_open(request: Request, target: Target, body: OpenBody) -> dict:
    _require(request)
    if target == "mac":
        proc = await run_in_threadpool(subprocess.run, ["open", "-a", body.name])
        if proc.returncode != 0:
            raise HTTPException(status_code=422, detail="app not found")
    else:
        _windows_online()
        reply = await agent_roundtrip(voice_router.request_envelope("open", {"name": body.name}))
        if not reply.get("ok"):
            raise HTTPException(status_code=422, detail=str(reply.get("data") or "app not found"))
    return {"ok": True}
