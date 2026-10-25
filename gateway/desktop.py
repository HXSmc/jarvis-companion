"""Desktop Jarvis Companion: the phone app's screens as one page for a PC browser.

Login is the /screen pattern: a session holding the ios_token mints a 60 s
single-use link (POST /desktop/launch); opening it trades the nonce for an
HttpOnly/Secure/SameSite=Strict `__Host-` cookie (7 days) bound to the
client's tailnet IP, and redirects to the clean /desktop URL. The browser never
sees the ios_token.

Why the IP binding: ts.net is a public suffix, so every https origin on the
tailnet is "same site" (SameSite does not stop them) and browsers send the
cookie to every port of this host (:8801, :8802...). A cookie copied off
another port is useless from any other tailnet node. tailscale serve sets
X-Forwarded-For itself and uvicorn --proxy-headers turns it into scope client.

`DesktopAuth` (ASGI middleware) turns that cookie into the ios_token bearer,
but only for the routes the phone app itself calls (ALLOWED below) and only
when the request carries no Authorization of its own. Unsafe methods also need
the `X-Jarvis-Desktop: 1` header (a cross-site form cannot set it; SameSite
Strict already keeps the cookie off cross-site requests), and a websocket
needs a same-origin Origin. Not covered: /hwmode (phone camera), /ws (the
phone's command socket), /cards/* (box sessions), /inbox/notify, /agents.

Sessions persist as sha256 digest -> [expiry, ip] in ~/.jarvis/desktop_sessions.json (0600),
so a gateway restart does not log the PC out. Revoke all: delete that file and
restart the gateway. Revoke one: the page's Log out button.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from common import config
from gateway import screen
from gateway.auth import require_bearer_any

logger = logging.getLogger(__name__)

COOKIE = "__Host-jarvis_desktop"
SESSION_TTL_S = 7 * 86400
SESSION_MAX = 5
BOOT_TTL_S = 60.0
CSRF_HEADER = b"x-jarvis-desktop"

STATIC = Path(__file__).resolve().parent / "static"
PAGE_HTML = STATIC / "desktop.html"
FLOOR_HTML = STATIC / "floor.html"

PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data: blob:; frame-src 'self' blob:; "
    "object-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# The floor page's own CSP, except it may sit in the desktop page's frame.
FLOOR_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data: 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)
# The iOS app injects window.__JARVIS + a webkit handler; here same-origin
# cookies carry auth, and "Open chat" goes to the parent desktop page.
FLOOR_SHIM = (
    "<script>window.__JARVIS=Object.freeze({base:\"\"});"
    "window.webkit={messageHandlers:{jarvis:{postMessage:function(m){"
    "parent.postMessage(m,location.origin);}}}};</script>"
)

# Exact (method, path) pairs the cookie may stand in for: the routes the phone
# app calls. tests/test_desktop.py walks app.routes so a new route under a
# matched prefix cannot slip in unnoticed. /screen is the page only: the page
# then mints its own screen_session cookie for frames/input/VNC.
_ID = r"[A-Za-z0-9-]{1,64}"
ALLOWED: list[tuple[str, re.Pattern[str]]] = [(m, re.compile(p)) for m, p in [
    ("POST", r"/chat"),
    ("GET", r"/central/sessions"),
    ("POST", r"/central/clear-done"),
    ("POST", r"/central/attachments"),
    ("GET", r"/central/agents"),
    ("GET", r"/central/watchdog"),
    ("GET", r"/central/feed"),
    ("POST", rf"/central/sessions/{_ID}/message"),
    ("POST", rf"/central/sessions/{_ID}/stop"),
    ("GET", rf"/central/sessions/{_ID}/events"),
    ("POST", rf"/central/cards/{_ID}/answer"),
    ("GET", rf"/central/cards/{_ID}/files/[A-Za-z0-9-]{{1,64}}"),
    ("POST", r"/siri/hey"),
    ("POST", r"/ask"),
    ("POST", r"/pc/wake"),
    ("GET", r"/inbox"),
    ("POST", r"/inbox/[^/]+/read"),
    ("GET", r"/notify/notes"),
    ("GET", r"/screen"),
]]
SAFE_METHODS = frozenset({"GET", "HEAD"})

_BOOTS: dict[str, float] = {}
_sessions: dict[str, list] | None = None  # sha256 hex -> [unix expiry, client ip]; loaded lazily


def _store_path() -> Path:
    return Path(os.environ.get("JARVIS_DESKTOP_SESSIONS_PATH", "~/.jarvis/desktop_sessions.json")).expanduser()


def _digest(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()


def _load() -> dict[str, list]:
    global _sessions
    if _sessions is None:
        try:
            raw = json.loads(_store_path().read_text())
            _sessions = {k: [float(v[0]), str(v[1])] for k, v in raw.items()}
        except (OSError, ValueError, AttributeError, TypeError, IndexError, KeyError):
            _sessions = {}
    now = time.time()
    for key in [k for k, (exp, _ip) in _sessions.items() if exp <= now]:
        del _sessions[key]
    return _sessions


def _save() -> None:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # O_EXCL: a leftover tmp never keeps a looser mode
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(_load(), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:  # sessions still work until the next restart
        logger.warning("desktop sessions not saved: %s", type(exc).__name__)


def mint_session(ip: str) -> str:
    sessions = _load()
    while len(sessions) >= SESSION_MAX:
        del sessions[min(sessions, key=lambda k: sessions[k][0])]
    session = secrets.token_urlsafe(32)
    sessions[_digest(session)] = [time.time() + SESSION_TTL_S, ip]
    _save()
    return session


def session_valid(session: str | None, ip: str | None) -> bool:
    """A live session, used from the same client IP that opened the link."""
    if not session or not ip:
        return False
    digest = _digest(session)
    return any(hmac.compare_digest(digest, known) and bound == ip for known, (_exp, bound) in _load().items())


def _client_ip(scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


def end_session(session: str | None) -> None:
    if session and _load().pop(_digest(session), None) is not None:
        _save()


def mint_boot() -> str:
    now = time.time()
    for key in [k for k, exp in _BOOTS.items() if exp <= now]:
        del _BOOTS[key]
    nonce = secrets.token_urlsafe(24)
    _BOOTS[nonce] = now + BOOT_TTL_S
    return nonce


def take_boot(nonce: str) -> bool:
    expiry = _BOOTS.pop(nonce, None)
    return expiry is not None and expiry > time.time()


def _cookie(headers: list[tuple[bytes, bytes]]) -> str | None:
    for key, value in headers:
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE:
                    return val
    return None


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    return next((v for k, v in headers if k == name), None)


def _allowed(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in ALLOWED)


def _same_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    origin, host = _header(headers, b"origin"), _header(headers, b"host")
    return bool(origin and host and origin.split(b"://", 1)[-1] == host)


class DesktopAuth:
    """Cookie -> ios_token bearer for the allowlisted phone routes (see module doc)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = scope["headers"]
            method = scope.get("method", "GET")
            if (
                _header(headers, b"authorization") is None
                and _allowed(method, scope["path"])
                and (
                    _same_origin(headers) if scope["type"] == "websocket"
                    else method in SAFE_METHODS or _header(headers, CSRF_HEADER) == b"1"
                )
                and session_valid(_cookie(headers), _client_ip(scope))
            ):
                token = config.get_token(name="ios_token")
                if token:
                    scope = dict(scope, headers=[*headers, (b"authorization", f"Bearer {token}".encode())])
        await self.app(scope, receive, send)


router = APIRouter()

LOCKED_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Jarvis</title>
<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#0a0a0c;color:#f2f2f4;
font-family:system-ui,-apple-system,"Segoe UI",sans-serif}div{max-width:420px;padding:24px;text-align:center}
h1{font-size:15px;letter-spacing:.2em;color:#8a8a93}p{color:#8a8a93;line-height:1.5}</style></head>
<body><div><h1>JARVIS</h1><p>This link has expired or was already used.</p>
<p>Ask Jarvis for a new desktop link.</p></div></body></html>"""


def _locked() -> HTMLResponse:
    return HTMLResponse(LOCKED_HTML, status_code=401, headers={"Cache-Control": "no-store"})


@router.get("/desktop")
async def desktop_page(request: Request, boot: str | None = None) -> Response:
    if boot is not None:
        if not take_boot(boot):
            return _locked()
        # Clean URL: the used nonce does not stay in the address bar or history.
        response = RedirectResponse("/desktop", status_code=303)
        response.set_cookie(COOKIE, mint_session(_client_ip(request.scope) or ""), max_age=SESSION_TTL_S, httponly=True,
                            secure=True, samesite="strict", path="/")
        return response
    if not session_valid(request.cookies.get(COOKIE), _client_ip(request.scope)):
        return _locked()
    return HTMLResponse(PAGE_HTML.read_text(),
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": PAGE_CSP})


@router.get("/desktop/floor")
async def desktop_floor(request: Request) -> Response:
    if not session_valid(request.cookies.get(COOKIE), _client_ip(request.scope)):
        return _locked()
    html = FLOOR_HTML.read_text().replace("<head>", "<head>" + FLOOR_SHIM, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": FLOOR_CSP})


@router.post("/desktop/logout")
async def desktop_logout(request: Request) -> Response:
    if request.headers.get("x-jarvis-desktop") != "1":
        raise HTTPException(status_code=403, detail="missing desktop header")
    end_session(request.cookies.get(COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@router.post("/desktop/launch", dependencies=[Depends(require_bearer_any("ios_token"))])
async def desktop_launch() -> dict:
    """A 60 s single-use login link; the nonce is never logged."""
    return {"url": f"{await screen._public_base()}/desktop?boot={mint_boot()}"}
