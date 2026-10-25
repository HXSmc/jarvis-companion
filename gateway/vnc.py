"""Mini PC VNC proxy: noVNC asset cache + auth-translating websocket to websockify.

Design: docs/superpowers/specs/2026-10-04-remote-screen-control-design.md (S1).
The Mini PC runs noVNC/websockify behind tailscale serve; the gateway translates
the phone's screen auth (screen_session cookie or ios Bearer — same rule as
gateway/screen.py, shared token 403'd) into the VNC password upstream via the
RFB 003.008 DES challenge-response. `SCREEN_CONTROL=0` unmounts this router too.
The VNC password value never appears in logs, exception messages, or responses.

Pin facts: noVNC assets are pinned against gateway/static/novnc-sha256.json;
MAX_VNC_SESSIONS caps concurrent /screen/minipc/ws sessions (extra -> 4004);
HANDSHAKE_TIMEOUT bounds both RFB handshake phases (upstream and client) —
upstream-phase timeout closes 4002, client-phase 1008.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.parse
from pathlib import Path

import httpx
import pyDes
import websockets
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket

from common import config
from gateway import screen

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/screen/minipc")

# Deployment-private upstream URLs: env only (see .env.example), no hardcoded
# default. Unset -> None, and every consumer fails loudly naming the var.
MINIPC_NOVNC_BASE = os.environ.get("MINIPC_NOVNC_BASE", "").strip().rstrip("/") or None
MINIPC_VNC_WS = os.environ.get("MINIPC_VNC_WS", "").strip() or None

HANDSHAKE_TIMEOUT = 10.0  # seconds, per RFB handshake phase
MAX_VNC_SESSIONS = 2  # concurrent /screen/minipc/ws sessions; extra -> 4004

NOVNC_NOT_FOUND = "noVNC asset not found"
NOVNC_UPSTREAM_DOWN = "noVNC upstream unreachable"
NOVNC_HASH_MISMATCH = "noVNC asset hash mismatch"

# Loaded once at import; a missing/invalid file fails the whole app startup
# on purpose — the asset proxy must never serve unpinned noVNC code.
_sha256_manifest: dict[str, str] = json.loads(
    (Path(__file__).resolve().parent / "static" / "novnc-sha256.json").read_text()
)


def vnc_password() -> str | None:
    return config.get_token(name="MINIPC_VNC_PASSWORD")


# ponytail: unbounded cache, fine (noVNC is ~60 files, fetched once per gateway life).
_cache: dict[str, bytes] = {}


async def online() -> bool:
    """HTTPS HEAD of the noVNC page; False on any exception."""
    if not MINIPC_NOVNC_BASE:
        logger.error("online: MINIPC_NOVNC_BASE is not set (copy .env.example to .env)")
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.head(f"{MINIPC_NOVNC_BASE}/vnc.html")
    except Exception:
        return False
    return resp.status_code == 200


async def _fetch(path: str) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{MINIPC_NOVNC_BASE}/{path}")
        return resp.status_code, resp.content


def _require_novnc_base() -> None:
    if not MINIPC_NOVNC_BASE:
        raise HTTPException(status_code=503,
                            detail="MINIPC_NOVNC_BASE is not set (copy .env.example to .env)")


@router.get("/novnc/{path:path}")
async def novnc_asset(request: Request, path: str) -> Response:
    screen._require(request)
    # L1+M3: manifest-pinned allowlist (replaces the prefix/suffix check) —
    # only exact paths in the import graph of core/rfb.js are proxyable, and
    # the served bytes must hash to the pinned sha256.
    if path not in _sha256_manifest:
        raise HTTPException(status_code=404, detail=NOVNC_NOT_FOUND)
    if path in _cache:
        return Response(_cache[path], media_type="text/javascript")
    _require_novnc_base()
    try:
        status, body = await _fetch(path)
    except Exception:
        raise HTTPException(status_code=502, detail=NOVNC_UPSTREAM_DOWN)
    if status != 200:
        raise HTTPException(status_code=404, detail=NOVNC_NOT_FOUND)
    if hashlib.sha256(body).hexdigest() != _sha256_manifest[path]:
        raise HTTPException(status_code=502, detail=NOVNC_HASH_MISMATCH)
    _cache[path] = body
    return Response(body, media_type="text/javascript")


def vnc_des(password: str, challenge: bytes) -> bytes:
    """RFB VNC auth: DES-ECB the 16-byte challenge under the bit-reversed key."""
    key = password.encode()[:8].ljust(8, b"\0")
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in key)
    return pyDes.des(key, pyDes.ECB).encrypt(challenge)


_live = 0  # current concurrent vnc_ws sessions (L2 cap)


@router.websocket("/ws")
async def vnc_ws(ws: WebSocket) -> None:
    global _live
    # screen._authorized only touches .cookies/.headers — a WebSocket carries
    # both, so the same helper serves HTTP and WS (no duplicate auth logic).
    kind = screen._authorized(ws)
    if kind not in ("cookie", "bearer"):
        await ws.close(code=1008)
        return
    # M2 cross-site WebSocket hijack guard: a cookie-authenticated connection
    # must prove same-origin (Origin netloc == the Host we serve, or the
    # tailscale serve base from screen._public_base). Bearer connections are
    # the iOS app — no Origin, no check.
    if kind == "cookie":
        origin = ws.headers.get("origin")
        allowed = {ws.headers.get("host", "")}
        base = await screen._public_base()
        allowed.add(urllib.parse.urlsplit(base).netloc)
        if not origin or urllib.parse.urlsplit(origin).netloc not in allowed:
            await ws.close(code=1008)
            return
    await ws.accept(subprotocol="binary")
    if _live >= MAX_VNC_SESSIONS:
        await ws.close(code=4004)
        return
    _live += 1
    up = None
    pumps: list[asyncio.Task] = []
    try:
        password = vnc_password()
        if password is None:
            await ws.close(code=4003)
            return
        if not MINIPC_VNC_WS:
            logger.error("vnc_ws: MINIPC_VNC_WS is not set (copy .env.example to .env)")
            await ws.close(code=4002)
            return
        try:
            # compression=None: RFB rects are already Tight/JPEG-compressed, so
            # permessage-deflate only adds CPU and delay per message.
            up = await websockets.connect(MINIPC_VNC_WS, subprotocols=["binary"], open_timeout=5,
                                          compression=None, max_size=None)
        except Exception:
            await ws.close(code=4002)
            return
        buf = bytearray()

        async def read(n: int) -> bytes:
            while len(buf) < n:
                buf.extend(await up.recv())
            out = bytes(buf[:n])
            del buf[:n]
            return out

        # Real client handshake against the upstream server. VNC auth (2) is
        # preferred over None (1) so the password is actually verified — the
        # spec's sketch listed 1 first, but its own S1 test offers {None, VNC}
        # upstream and expects the DES challenge-response.
        async def upstream_phase() -> None:
            await read(12)  # server version
            await up.send(b"RFB 003.008\n")
            n = (await read(1))[0]
            if n == 0:
                raise RuntimeError("server sent no security types")
            types = await read(n)
            if 2 in types:
                await up.send(b"\x02")
                await up.send(vnc_des(password, await read(16)))
            elif 1 in types:
                await up.send(b"\x01")
            else:
                raise RuntimeError("unsupported security types")
            if int.from_bytes(await read(4), "big") != 0:
                raise RuntimeError("upstream auth refused")

        # Fake the server side for the browser's noVNC client: plain None auth.
        async def client_phase() -> None:
            await ws.send_bytes(b"RFB 003.008\n")
            await ws.receive_bytes()  # client version
            await ws.send_bytes(b"\x01\x01")  # one security type: None
            await ws.receive_bytes()  # client's choice
            await ws.send_bytes(b"\x00\x00\x00\x00")  # SecurityResult OK

        try:
            await asyncio.wait_for(upstream_phase(), HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.close(code=4002)
            return
        except Exception:
            await ws.close(code=4001)
            return
        try:
            await asyncio.wait_for(client_phase(), HANDSHAKE_TIMEOUT)
        except Exception:
            await ws.close(code=1008)
            return

        if buf:  # leftover upstream bytes (e.g. ServerInit) reach the client
            await ws.send_bytes(bytes(buf))  # before the pumps start (M1).

        async def client_to_up() -> None:
            while True:
                await up.send(await ws.receive_bytes())

        async def up_to_client() -> None:
            while True:
                await ws.send_bytes(await up.recv())

        pumps.append(asyncio.create_task(client_to_up()))
        pumps.append(asyncio.create_task(up_to_client()))
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pumps:
            if task.done() and not task.cancelled():
                task.exception()
    finally:
        # M1 resource safety: every path after accept cancels the pumps and
        # closes both legs — no leaked task or socket on cancel/return/error.
        _live -= 1
        for task in pumps:
            task.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)
        if up is not None:
            try:
                await up.close()
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass
