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

Attachments (gateway/attachments.py): POST /central/attachments stores one
file; /chat and session messages take `attachments: [id]` and the gateway
appends the files' local paths to the text for the session to Read.

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
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from common import config
from gateway import attachments, sprites
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
    "session_id", "kind", "title", "state", "cwd", "updated_at", "device",
    "project", "task", "summary", "phase", "last_activity_at", "health",
    # Ledger fields (session e7c7438a, live): dropped before this pass added
    # them to the allowlist, so the Floor/board never saw them.
    "purpose", "spawned_by", "parent_session_id", "lane", "lifecycle",
    "blocked_reason", "done_reason", "throwaway", "current_step_index",
    "last_failed_step", "engine",
    # Watchdog + role fields (router session ce03d92a, NOT confirmed live as
    # of this pass — see .orchestrator/reports/watchdog-roles-app-side.md).
    # Listed here so they pass through the day the router starts sending
    # them; until then _pick() just never finds the key and drops it.
    "role", "attention_reason", "done_verdict", "done_verdict_reason",
)
_PHASES = ("in_progress", "in_review", "done")
# New lifecycle health enum (contract): working | quiet | waiting | idle |
# done | attention | failed. Used only when the router sends no `health` yet.
_HEALTH_FALLBACK = {"in_progress": "working", "in_review": "waiting"}
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
    "connect-src 'self'; img-src data: 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

AGENTIC_OS_URL_DEFAULT = "http://127.0.0.1:8787"
AGENTIC_OS_TOKEN_PATH = FilePath.home() / ".config" / "agentic-os" / "token"
AGENTIC_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_HUB_KEYS = ("key", "dirLabel", "engine", "active", "lastTs")
_AGENT_KEYS = ("id", "persona", "engine", "label", "active", "status")

# Split deploy (gateway on the mini PC): agentic-os's dashboard only runs on
# the Mac at 127.0.0.1:8787, same reason gateway/screen.py proxies
# /screen/mac/* back to the Mac. MAC_GATEWAY_URL unset = this process runs on
# the Mac and can reach agentic-os directly; set = forward to the Mac's own
# /central/agents (which does the direct call) with the ios_token.
MAC_GATEWAY_ENV = "MAC_GATEWAY_URL"
MAC_PROXY_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def _mac_gateway() -> str | None:
    return os.environ.get(MAC_GATEWAY_ENV, "").rstrip("/") or None


def _mac_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Outbound client to the Mac gateway (tests swap in an ASGI transport here)."""
    return httpx.AsyncClient(timeout=timeout)


def _ios_token() -> str | None:
    """The ios_token bearer used to call the Mac gateway (tests stub this)."""
    return config.get_token(name="ios_token")


async def _mac_agents() -> dict | None:
    """GET the Mac gateway's own /central/agents (direct agentic-os call); None on any failure."""
    token = _ios_token()
    if not token:
        return None
    try:
        async with _mac_client(MAC_PROXY_TIMEOUT) as client:
            resp = await client.get(
                f"{_mac_gateway()}/central/agents", headers={"Authorization": f"Bearer {token}"}
            )
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("mac gateway /central/agents failed: %s", type(exc).__name__)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("hubs"), list):
        return None
    out = {"device": data.get("device") if isinstance(data.get("device"), str) else None, "hubs": data["hubs"]}
    if data.get("stale") is True:
        out["stale"] = True
    return out


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
    out["device"] = out.get("device") or DEVICE  # router sets "Mac" for Mac-hook rows
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
    # Never hide a session that needs the owner: the old "in review" signal,
    # the legacy literal "waiting_for_owner" health value (pre-rename router),
    # the new health enum's waiting/attention, plus a done session whose
    # done_verdict says the DONE might be false (done_verdict == "suspect").
    needs_owner = (
        out.get("state") == "needs_input"
        or out["phase"] == "in_review"
        or out["health"] in ("waiting", "attention", "waiting_for_owner")
        or (out["phase"] == "done" and out.get("done_verdict") == "suspect")
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


class MessageBody(BaseModel):
    msg_id: str = Field(pattern=ID_PATTERN)
    # Empty text is fine when files are attached (a photo with no words).
    text: str = Field(default="", max_length=4000)
    attachments: list[str] = Field(default_factory=list, max_length=attachments.MAX_PER_MESSAGE)

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.text.strip() and not self.attachments:
            raise ValueError("text or attachments required")
        return self


class ChatBody(MessageBody):
    thread_id: str = Field(pattern=ID_PATTERN)


def _with_files(body: MessageBody) -> str:
    """Message text plus the attachment paths block; unknown id -> 400 before any router call."""
    try:
        if not body.attachments:
            return body.text
        return (body.text + attachments.note(body.attachments)).strip()
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError:  # the file vanished between resolve and stat
        raise HTTPException(status_code=400, detail="attachment not found") from None


@router.post("/central/attachments")
async def upload_attachment(request: Request, name: str = Query(default="", max_length=200)) -> dict:
    """One file as the raw body (its Content-Type); stored for the next message to reference."""
    try:
        kind = attachments.media_type(request.headers.get("content-type"))
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > attachments.MAX_BYTES:
            raise attachments.AttachmentError("file too large")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > attachments.MAX_BYTES:
                raise attachments.AttachmentError("file too large")
        return attachments.save(bytes(data), name, kind)
    except attachments.AttachmentError as exc:
        status = 413 if str(exc) == "file too large" else 415 if "type" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from None
    except OSError as exc:
        logger.warning("attachment save failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="could not save") from None


@router.post("/chat")
async def chat(body: ChatBody) -> dict:
    text = _with_files(body)
    try:
        return await route(body.thread_id, body.msg_id, text, "app")
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


# Not shipped by the router as of this pass (router session ce03d92a; GET
# /watchdog answers 404) — WATCHDOG_STALE_S + this route exist so the Floor's
# banner lights up the day it does, without another deploy.
WATCHDOG_STALE_S = 180
_EMPTY_WATCHDOG = {
    "available": False, "stale": None, "age_s": None,
    "last_sweep_at": None, "sweep_errors_last_hour": None, "alerts": [],
}


@router.get("/central/watchdog")
async def watchdog() -> dict:
    """Router watchdog sweep status, with staleness computed here (the box's
    clock matches the router's; the phone's may not). A router that doesn't
    serve /watchdog yet (404) or is unreachable both read as "unavailable" —
    distinct from "available but stale", which is the actual alert state.
    """
    try:
        data = await _router_json("GET", "/watchdog")
    except RouterError:
        return dict(_EMPTY_WATCHDOG)
    if not isinstance(data, dict):
        return dict(_EMPTY_WATCHDOG)
    last_sweep = _parse_ts(data.get("last_sweep_at"))
    age_s = (_now() - last_sweep).total_seconds() if last_sweep else None
    alerts = data.get("alerts")
    return {
        "available": True,
        "last_sweep_at": data.get("last_sweep_at"),
        "sweep_errors_last_hour": data.get("sweep_errors_last_hour"),
        "alerts": [a for a in alerts if isinstance(a, dict)] if isinstance(alerts, list) else [],
        "age_s": age_s,
        "stale": age_s is not None and age_s > WATCHDOG_STALE_S,
    }


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
    text = _with_files(body)
    try:
        data = await _router_json(
            "POST", f"/sessions/{session_id}/message", {"msg_id": body.msg_id, "text": text}
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


# Role sprites (Ask 2). Ownership split (fable 2, 2026-10-16): the real art
# — per-role SVG + PNG @1x/2x/3x, a combined sprites.svg with <symbol
# id="sprite-ROLE">, preview.html — lives under gateway/static/sprites/, built
# in fable 2's own worktree (~/Desktop/personal/jarvis-companion-sprites,
# branch `sprites`). This file never creates or edits anything in that
# directory. What's wired here: the route serves a static file from there
# when one exists, and falls back to gateway/sprites.py's procedural
# generator (SVG natively; PNG via its hand-rolled encoder) when it doesn't —
# so the Floor and the Sessions board both render something today and pick up
# the real art automatically the moment fable 2 merges it, no redeploy of
# this logic needed. Public like /central/floor: an <img>/AsyncImage/Canvas
# fetch can't attach a bearer, and a sprite carries no data or secret. `role`
# is looked up in sprites.ROLES (no path join, no arbitrary file access),
# falling back to WORKER for anything unknown — the route itself never 404s
# so a stale/unexpected role string can't blank a card.
SPRITE_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400, immutable"}
SPRITES_STATIC_DIR = FilePath(__file__).resolve().parent / "static" / "sprites"


def _static_roles() -> set[str]:
    """Roles fable 2's real art currently covers, from her roles.json
    (`[{"role": "...", "label": "...", "file": "..."}, ...]`) — read fresh
    each call like FLOOR_HTML.read_text() above (and from SPRITES_STATIC_DIR
    at call time, not a path baked in at import, so tests can point it at a
    tmp dir), so a v2 roles.json (adds EXPLORER/WORKER per her note) needs no
    code change here to be picked up. Missing/unreadable file -> empty set,
    not an error (v1 might not ship it, or the directory isn't merged yet)."""
    try:
        data = json.loads((SPRITES_STATIC_DIR / "roles.json").read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {item["role"] for item in data if isinstance(item, dict) and isinstance(item.get("role"), str)}


def _sprite_role(role: str) -> str:
    """Normalize to a role this route can serve something for. Prefers her
    roles.json (not a hardcoded list) when the role is one she's shipped art
    for; otherwise falls back to sprites.ROLES, the generator's own role set
    (which already covers EXPLORER/WORKER/etc — this is what keeps those
    roles working today, before her v2 roles.json adds them); anything else
    normalizes to WORKER so the route never 404s."""
    up = role.upper()
    if up in _static_roles() or up in sprites.ROLES:
        return up
    return "WORKER"


@public_router.get("/central/sprites/{role}.svg")
async def sprite_svg(role: str = Path(pattern=r"^[A-Za-z_]{1,32}$")) -> Response:
    looked_up = _sprite_role(role)
    art_file = SPRITES_STATIC_DIR / f"{looked_up}.svg"
    if art_file.is_file():
        return Response(art_file.read_bytes(), media_type="image/svg+xml", headers=SPRITE_CACHE_HEADERS)
    return Response(sprites.svg_for(looked_up), media_type="image/svg+xml", headers=SPRITE_CACHE_HEADERS)


def _sprite_png_candidates(role: str, scale: str) -> tuple[str, ...]:
    """fable 2's build_sprites.py names the 1x file `{role}.png` (bare, no
    `@1x`) and only `@2x`/`@3x` carry a scale suffix. Check that real name
    first; `{role}@1x.png` stays a defensive second match in case that ever
    changes, so this route doesn't silently fall back to the generator."""
    if scale == "1":
        return (f"{role}.png", f"{role}@1x.png")
    return (f"{role}@{scale}x.png",)


@public_router.get("/central/sprites/{role}@{scale}x.png")
async def sprite_png(
    role: str = Path(pattern=r"^[A-Za-z_]{1,32}$"), scale: str = Path(pattern=r"^[123]$")
) -> Response:
    looked_up = _sprite_role(role)
    for name in _sprite_png_candidates(looked_up, scale):
        art_file = SPRITES_STATIC_DIR / name
        if art_file.is_file():
            return Response(art_file.read_bytes(), media_type="image/png", headers=SPRITE_CACHE_HEADERS)
    return Response(
        sprites.png_bytes_for(looked_up, sprites.PNG_SCALES[f"{scale}x"]),
        media_type="image/png", headers=SPRITE_CACHE_HEADERS,
    )


@public_router.get("/central/sprites")
async def sprite_roles() -> dict:
    """The role list, so a client can prefetch/cache every sprite up front."""
    return {"roles": list(sprites.ROLES)}


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


# Last good Mac snapshot. The Mac sleeps / drops off the tailnet often; without
# this the Floor's Mac office vanished on the first failed poll and never came
# back until the Mac woke. Served (hubs marked idle, stale=True) for this long.
AGENTS_KEEP_S = 12 * 3600
_last_agents: dict = {"data": None, "at": 0.0}


async def _fetch_agents() -> dict | None:
    """One live snapshot: Mac gateway in box mode, agentic-os directly on the Mac; None = unreachable."""
    if _mac_gateway():
        return await _mac_agents()
    token = agentic_os_token()
    if not token:
        return None
    try:
        async with _agentic_client(AGENTIC_TIMEOUT) as client:
            resp = await client.get(
                f"{agentic_os_base()}/api/snapshot", headers={"Authorization": f"Bearer {token}"}
            )
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("agentic-os snapshot failed: %s", type(exc).__name__)
        return None
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return None
    hubs = sessions.get("hubs")
    device = sessions.get("device")
    return {
        "device": device if isinstance(device, str) else None,
        "hubs": [_hub(h) for h in hubs if isinstance(h, dict)] if isinstance(hubs, list) else [],
    }


@router.get("/central/agents")
async def agents() -> dict:
    """The Mac's hub sessions + their agents from agentic-os, trimmed.

    When this process runs on the box (MAC_GATEWAY_URL set), agentic-os is not
    reachable locally — forward to the Mac gateway's own /central/agents
    instead of calling agentic-os directly. Unreachable -> the last good
    snapshot (up to AGENTS_KEEP_S old) with every hub/agent idle and
    stale=True + age_s, else no hubs.
    """
    data = await _fetch_agents()
    now = time.monotonic()
    if data is not None and not data.get("stale"):
        _last_agents.update(data=data, at=now)
        return data
    cached, age = _last_agents["data"], now - _last_agents["at"]
    if cached is None or age > AGENTS_KEEP_S:
        return data or {"device": None, "hubs": []}
    idle = lambda item: {**item, "active": False}  # noqa: E731
    return {
        "device": cached["device"],
        "hubs": [{**idle(h), "agents": [idle(a) for a in h.get("agents") or []]} for h in cached["hubs"]],
        "stale": True,
        "age_s": int(age),
    }