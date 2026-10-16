"""Central chat: an explicit allowlist proxy from the iPhone app to the mini PC router.

Contract: .orchestrator/inbox/fable2-central-chat-framing.md (router API §2 +
addendum §6). Every route here takes the Bearer `ios_token` only — never the
shared token — and maps onto exactly one router call; there is no generic
passthrough. The router bearer (`MINIPC_ROUTER_TOKEN`, vault `## Jarvis
Companion`) is read per request through `common.config.get_token` and is never
logged, echoed, or returned. Router failures surface as a 502 with a short
generic detail (never the upstream body).

Plain words (round 3): each session also carries `device` (constant "Mini PC"),
`doing` (one plain line) and `recap` (short plain bullets) made by a local
`claude -p` call (Haiku, the owner's Claude subscription — never OpenRouter,
never an API key) from the latest summary, plus `floor_hidden` (idle 30+ min,
UTC, never for a session that needs the owner). The router's `last_activity_at`
and `health` pass through unchanged; when missing they fall back to
`updated_at` and to the phase (working / waiting_for_owner / null). The model runs in the
background only; the 10 s poll reads the cache in ~/.jarvis/central_state.json
(keyed by session id + summary hash) and falls back to the router title.
POST /central/clear-done stores one `cleared_before` UTC time there; done
sessions last updated at or before it are left out of /central/sessions.

Beyond the contract: GET /central/feed relays the router's GET /feed (SSE, one
event per line of {seq, ts, session_id, type, from, to, text}) for the Agency
Floor. The framing doc does not define /feed yet; until the router serves it,
/central/feed answers 502.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path as FilePath

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from common import config
from gateway.auth import require_bearer_any
from mac.voice import brain as _brain

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
# `seq` (when the router sends it) is the event seq of the reply, so the phone
# resumes the session stream after it instead of rendering the reply twice.
_ROUTE_KEYS = ("action", "session_id", "kind", "title", "reply", "prompt", "options", "seq")
_SESSION_KEYS = (
    "session_id", "kind", "title", "state", "cwd", "updated_at",
    "project", "task", "summary", "phase", "last_activity_at", "health",
)
_PHASES = ("in_progress", "in_review", "done")
# Used only when the router sends no `health` yet (pre-patch router); done -> None.
_HEALTH_FALLBACK = {"in_progress": "working", "in_review": "waiting_for_owner"}
# Used only when the router sends no valid `phase`; an unknown state is in_progress.
_PHASE_FALLBACK = {
    "running": "in_progress", "needs_input": "in_review",
    "done": "done", "failed": "done", "idle": "done", "stopped": "done",
}

DEVICE = "Mini PC"
FLOOR_IDLE = timedelta(minutes=30)
STATE_PATH = FilePath.home() / ".jarvis" / "central_state.json"
# launchd's PATH may lack `claude` (it isn't a login shell) — resolve once at
# import time, falling back to the known absolute path from `which claude`.
_CLAUDE_BIN = shutil.which("claude") or "/Users/<user>/.local/bin/claude"
PLAIN_TIMEOUT_SECONDS = 60.0
PLAIN_RETRY_SECONDS = 600  # a failed generation for one summary is not retried sooner
DOING_MAX = 80
RECAP_MAX_ITEMS = 6
RECAP_ITEM_MAX = 140
PLAIN_INSTRUCTION = (
    "You explain what an AI helper session is doing, for a busy owner who is not technical. "
    'Reply with JSON only: {"doing": "...", "recap": ["...", "..."]}. '
    '"doing": one short line, under 70 characters, saying what it is doing right now in plain words. '
    '"recap": 1 to 5 short bullets in first person past tense, like "I fixed the login page" '
    'or "I downloaded the photos". If the owner must act, the last bullet starts with "Needs you: ". '
    "No jargon, no code, no file paths, no markdown."
)

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


def _now() -> datetime:
    """Current UTC time (the seam tests pin)."""
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    """Router ISO time -> aware UTC datetime; naive means UTC; junk -> None."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_state() -> dict:
    """{"cleared_before": iso | None, "plain": {sid: {hash, doing, recap}}}; missing/bad file -> empty."""
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    cleared = data.get("cleared_before")
    plain = data.get("plain")
    return {
        "cleared_before": cleared if isinstance(cleared, str) else None,
        "plain": plain if isinstance(plain, dict) else {},
    }


def _save_state(state: dict) -> None:
    """Atomic write, private like wake.py's state: dir 0o700, file 0o600. Raises OSError."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def _summary_hash(summary: str) -> str:
    return hashlib.sha256(summary.encode()).hexdigest()[:16]


def _session(item: dict, plain: dict | None = None, now: datetime | None = None) -> dict:
    out = _pick(item, _SESSION_KEYS)
    if out.get("phase") not in _PHASES:
        out["phase"] = _PHASE_FALLBACK.get(out.get("state"), "in_progress")
    out["device"] = DEVICE
    # Router values pass through unchanged; fill only what an older router does not send.
    if not out.get("last_activity_at"):
        out["last_activity_at"] = out.get("updated_at")
    if not out.get("health"):
        out["health"] = _HEALTH_FALLBACK.get(out["phase"])
    # A cached line for an older summary still beats the raw title while the new one is made.
    hit = (plain or {}).get(out.get("session_id"))
    if isinstance(hit, dict) and isinstance(hit.get("doing"), str):
        out["doing"] = hit["doing"]
        out["recap"] = [r for r in hit.get("recap") or [] if isinstance(r, str)]
    else:
        out["doing"] = out.get("title") or out.get("task") or out.get("session_id")
        out["recap"] = []
    ts = _parse_ts(out.get("last_activity_at"))
    needs_owner = (
        out.get("state") == "needs_input" or out["phase"] == "in_review" or out["health"] == "waiting_for_owner"
    )
    out["floor_hidden"] = bool(ts and not needs_owner and (now or _now()) - ts >= FLOOR_IDLE)
    return out


def _cleared(session: dict, cleared_before: datetime | None) -> bool:
    """Done and last updated at/before the clear (no time -> treated as old)."""
    if cleared_before is None or session["phase"] != "done":
        return False
    ts = _parse_ts(session.get("updated_at"))
    return ts is None or ts <= cleared_before


# --- plain-words generation (background only; never awaited by the poll) -------

_plain_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-plain")
_inflight: set[str] = set()
_failed: dict[str, float] = {}  # "sid:hash" -> monotonic time of the last failure
_tasks: set[asyncio.Task] = set()  # strong refs so running workers are not garbage collected
_known_ids: set[str] = set()  # session ids in the latest poll; the cache is pruned to these


def _plain_line(value, limit: int) -> str:
    """One plain line: markdown marks and bullet prefixes stripped, spaces folded, capped."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[*`#>]+", "", value)
    text = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean_plain(text: str) -> dict | None:
    """Model reply -> {"doing", "recap"} or None when it is not usable JSON."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    doing = _plain_line(data.get("doing"), DOING_MAX)
    if not doing:
        return None
    raw = data.get("recap")
    items = raw if isinstance(raw, list) else []
    recap = [line for line in (_plain_line(r, RECAP_ITEM_MAX) for r in items) if line]
    return {"doing": doing, "recap": recap[:RECAP_MAX_ITEMS]}


def _run_claude_plain(prompt: str, timeout_s: float) -> str | None:
    """One blocking `claude -p` call (Haiku, owner's subscription) -> raw stdout, or None.

    Same hardened shape as mac/voice/brain.run_claude and
    gateway/quick.py's _claude_web_answer: prompt via stdin only (never
    argv), no tools/MCP/user settings, scratch cwd, minimal env, the shared
    slim config dir (~/.claude-jarvis-brain) for subscription auth — this
    Mac's ANTHROPIC_API_KEY is dead, so the subprocess env drops it (and
    ANTHROPIC_BASE_URL) entirely rather than ever trying it. Killed on
    timeout. The seam tests monkeypatch.
    """
    argv = [
        _CLAUDE_BIN, "-p",
        "--model", "haiku",
        "--tools", "",
        "--mcp-config", '{"mcpServers":{}}',
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--append-system-prompt", PLAIN_INSTRUCTION,
    ]
    env = {key: os.environ[key] for key in _brain._BRAIN_ENV_KEYS if key in os.environ}
    env["CLAUDE_CONFIG_DIR"] = str(_brain.BRAIN_CONFIG_DIR)
    scratch = tempfile.mkdtemp(prefix="jarvis-plain-")
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True, text=True,
            cwd=scratch, env=env,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # missing binary, or killed by the timeout
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _generate_plain(session: dict) -> dict | None:
    """Blocking: one `claude -p` call -> {"doing", "recap"}; None when unusable."""
    facts = "\n".join(
        f"{label}: {session[k]}" for label, k in (("Title", "title"), ("Task", "task"), ("State", "state"))
        if isinstance(session.get(k), str) and session[k]
    )
    prompt = f"{facts}\nLatest summary:\n{session['summary'][:4000]}"
    text = _run_claude_plain(prompt, PLAIN_TIMEOUT_SECONDS)
    if text is None:
        return None
    return _clean_plain(text)


async def _plain_worker(sid: str, digest: str, session: dict) -> None:
    key = f"{sid}:{digest}"
    try:
        result = await asyncio.get_running_loop().run_in_executor(_plain_executor, _generate_plain, session)
        if result is None:
            _failed[key] = time.monotonic()
            return
        # Load + save run with no await between them, so workers never interleave on the file.
        state = _load_state()
        plain = {k: v for k, v in state["plain"].items() if k in _known_ids}
        plain[sid] = {"hash": digest, **result}
        state["plain"] = plain
        _save_state(state)
    except Exception as exc:  # a failed summary never breaks the gateway
        logger.info("plain summary for %s failed: %s", sid, type(exc).__name__)
        _failed[key] = time.monotonic()
    finally:
        _inflight.discard(key)


def _schedule_plain(sessions: list[dict], plain: dict) -> None:
    """Fire-and-forget generation for sessions whose summary is not cached yet."""
    now = time.monotonic()
    if len(_failed) > 1000:
        _failed.clear()
    loop = asyncio.get_running_loop()
    for s in sessions:
        sid, summary = s.get("session_id"), s.get("summary")
        if not isinstance(sid, str) or not isinstance(summary, str) or not summary.strip():
            continue
        digest = _summary_hash(summary)
        hit = plain.get(sid)
        key = f"{sid}:{digest}"
        if isinstance(hit, dict) and hit.get("hash") == digest:
            continue
        if key in _inflight or now - _failed.get(key, -PLAIN_RETRY_SECONDS) < PLAIN_RETRY_SECONDS:
            continue
        _inflight.add(key)
        task = loop.create_task(_plain_worker(sid, digest, s))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


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
    state = _load_state()
    now = _now()
    cleared_before = _parse_ts(state["cleared_before"])
    items = [item for item in data if isinstance(item, dict)]
    out = []
    raw = []
    for item in items:
        session = _session(item, state["plain"], now)
        if not _cleared(session, cleared_before):
            out.append(session)
            raw.append(_pick(item, _SESSION_KEYS))
    _known_ids.clear()
    _known_ids.update(s["session_id"] for s in out if isinstance(s.get("session_id"), str))
    _schedule_plain(raw, state["plain"])
    return out


@router.post("/central/clear-done")
async def clear_done() -> dict:
    """Hide every session that is done now; one that becomes active again reappears."""
    state = _load_state()
    state["cleared_before"] = _iso(_now())
    try:
        _save_state(state)
    except OSError as exc:
        logger.warning("central state save failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="could not save") from None
    return {"cleared_before": state["cleared_before"]}


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
    except Exception as exc:  # not only httpx errors: never leak the client or a 500
        await client.aclose()
        logger.warning("router GET %s failed: %s", path, type(exc).__name__)
        raise HTTPException(status_code=502, detail=ROUTER_UNAVAILABLE) from None
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
