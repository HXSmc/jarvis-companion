"""GET /overlay-log: the Companion's read-only window on overlay turns.

On the Mac (no MAC_GATEWAY_URL) it reads the local overlay service's
/overlay/debug/turns and redacts; on the mini PC it forwards to the Mac's
gateway with the ios_token and redacts again. Auth is screen._require
(ios_token only, the shared token is refused). Redaction is an allow-list:
only the step names and fields below survive, strings capped at 200 chars,
nested dicts/lists dropped — heard words, spoken lines, tool decisions and
timings only; never screen contents, tool output or card bodies.
"""

from __future__ import annotations

import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from common import config
from gateway import overlay, screen

router = APIRouter()

# Steps whose rows keep more than {step, at} (spec jfollow3 D1 + hunk/ground/layout).
KEEP_STEPS = frozenset({
    "wake", "voice", "transcribed", "continued", "intent", "conversation", "spoke", "done",
    "followup_dropped", "followup_closed", "barge_in", "media", "card_size", "sessions",
    "agent_confirm", "agent_progress", "agent_tool", "lines", "no_speech", "incomplete",
    "app_error", "hunk", "ground", "layout",
})
# String fields a kept row may hold; every other string field is dropped.
KEEP_FIELDS = frozenset({"step", "at", "detail", "text", "reason", "kind", "stt"})
MAX_STR = 200
MAX_TURNS = 50
NUMBERS = (bool, int, float)

OVERLAY_OFFLINE_DETAIL = "overlay offline"
OVERLAY_PORT_DEFAULT = 8791  # mac/voice/service.py binds OVERLAY_PORT, default 8791


def _cap(value):
    return value[:MAX_STR] if isinstance(value, str) else value


def _row(step: dict) -> dict:
    name = step.get("step")
    if name not in KEEP_STEPS:
        return {"step": name, "at": step.get("at")}
    row = {}
    for key, value in step.items():
        if name == "agent_tool":
            if key in ("step", "at", "tool", "decision") and isinstance(value, NUMBERS + (str, type(None))):
                row[key] = _cap(value)
        elif key in KEEP_FIELDS and isinstance(value, NUMBERS + (str, type(None))):
            row[key] = _cap(value)  # scalars only: a nested dict/list under a kept name is dropped
        elif isinstance(value, NUMBERS):  # ms/audio_ms/score-style counters
            row[key] = value
    return row


def _flat(mapping: dict) -> dict:
    """Scalar-only view (listener/setup/speech_gate): strings capped, the rest kept."""
    return {k: _cap(v) for k, v in mapping.items() if isinstance(v, NUMBERS + (str,))}


def redact(payload: dict, since: int | None = None) -> dict:
    """Allow-list one /overlay-log payload; pure — applied on the Mac and again after forwarding."""
    cutoff = time.time() - since if since else None
    turns = []
    for turn in payload.get("turns") or []:
        steps = turn.get("steps") or []
        start = steps[0].get("at") if steps else None
        if cutoff is not None and (not isinstance(start, NUMBERS) or start < cutoff):
            continue  # a turn with no start time is unfilterable -> dropped when since is given
        turns.append({"id": _cap(turn.get("id")), "steps": [_row(s) for s in steps]})
        if len(turns) >= MAX_TURNS:
            break
    out: dict = {"listener": _flat(payload.get("listener") or {}), "turns": turns}
    for key in ("setup", "speech_gate"):
        if isinstance(payload.get(key), dict):
            out[key] = _flat(payload[key])
    if "commit" in payload:
        out["commit"] = _cap(payload.get("commit"))
    return out


_overlay_http: httpx.AsyncClient | None = None
_mac_http: httpx.AsyncClient | None = None


def _overlay_client() -> httpx.AsyncClient:
    """Client to the Mac's local overlay service (tests swap in a MockTransport here)."""
    global _overlay_http
    if _overlay_http is None or _overlay_http.is_closed:
        _overlay_http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    return _overlay_http


def _mac_client() -> httpx.AsyncClient:
    """Outbound client to the Mac gateway (tests swap in an ASGI/Mock transport here)."""
    global _mac_http
    if _mac_http is None or _mac_http.is_closed:
        _mac_http = httpx.AsyncClient(timeout=screen.MAC_PROXY_TIMEOUT)
    return _mac_http


def _overlay_port() -> int:
    return int(os.environ.get("OVERLAY_PORT", str(OVERLAY_PORT_DEFAULT)))


async def fetch(since: int | None = None) -> dict:
    """The Mac-side read: the overlay service's debug turns, redacted, plus its commit."""
    headers = {"Authorization": f"Bearer {overlay.overlay_token()}"}
    base = f"http://127.0.0.1:{_overlay_port()}"
    try:
        resp = await _overlay_client().get(f"{base}/overlay/debug/turns", headers=headers)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail=OVERLAY_OFFLINE_DETAIL) from None
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail=OVERLAY_OFFLINE_DETAIL)
    try:
        turns = resp.json()
    except ValueError:
        raise HTTPException(status_code=503, detail=OVERLAY_OFFLINE_DETAIL) from None
    commit = None
    try:  # the installed overlay commit; the service may predate the field or be down
        commit = (await _overlay_client().get(f"{base}/overlay/health", headers=headers)).json().get("commit")
    except (httpx.HTTPError, ValueError):
        pass
    return redact({**turns, "commit": commit}, since)


async def _forward(since: int | None) -> dict:
    """Mini-PC side: the same route on the Mac's gateway, redacted again on arrival."""
    token = config.get_token(name="ios_token")
    if not token:
        raise HTTPException(status_code=503, detail=screen.MAC_OFFLINE_DETAIL)
    try:
        resp = await _mac_client().get(f"{screen._mac_gateway()}/overlay-log",
                                       params={"since": since} if since is not None else None,
                                       headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail=screen.MAC_OFFLINE_DETAIL) from None
    if resp.status_code == 200:
        try:
            return redact(resp.json(), since)
        except ValueError:
            pass  # not JSON from the Mac -> offline below
    elif resp.status_code == 422:  # version skew: the Mac's validation detail passes through
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        raise HTTPException(status_code=422, detail=detail or "invalid since")
    raise HTTPException(status_code=503, detail=screen.MAC_OFFLINE_DETAIL)


@router.get("/overlay-log", dependencies=[Depends(screen._require)])
async def get_overlay_log(since: int | None = Query(None, ge=1, le=86400)) -> dict:
    """Redacted overlay turns: forwarded when split-deployed (MAC_GATEWAY_URL), else read locally."""
    if screen._mac_gateway():
        return await _forward(since)
    return await fetch(since)
