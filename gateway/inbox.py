"""Inbox endpoints: serve J1's vendor-watch store read-only + mark-read + notify.

The store (``~/.jarvis/vendor-watch/inbox.json``, dir overridable via
``JARVIS_VENDOR_WATCH_HOME``) is written by J1's watcher; this module NEVER
writes items, NEVER summarises (summaries/next_steps are generated once at
ingest by J1) — it only lists, flips the read flag, and pushes an
``inbox_new`` command to the connected iOS socket. File access is serialised
with an fcntl lock on ``inbox.json.lock``; writes are atomic (tmp + rename).

J3 adds the guaranteed Mac-side path: notify with new mail also enqueues a
jarvis-phone outbound message (WhatsApp-to-self with a ``jarvis://inbox``
link; email instead while the overnight marker is set) and, when the caller
flags the mail urgent, a jarvis-phone event. The jarvis-phone root is
overridable via ``JARVIS_PHONE_DIR``; every enqueue failure is logged and
swallowed — notify must never raise.

Split deploy (gateway on the mini PC): J1's watcher (``scripts/vendor_watch.py``)
only ever runs on the Mac (it's the only machine with example MCP access), so
the box's own local store is always empty. When ``MAC_GATEWAY_URL`` is set
(box mode, same env + pattern as ``gateway/central.py`` and ``gateway/screen.py``),
``GET /inbox`` and the mark-read route forward to the Mac's own copy of those
routes with the ios_token instead of reading the local (empty) store.
``POST /inbox/notify`` stays local even in box mode — its job there is to
wake whichever "ios" websocket is connected to THIS gateway instance — but it
skips the guaranteed-path enqueue (the Mac's own local notify call already
ran that) and pulls the real unread count from the Mac instead of the local
store.

``POST /inbox/task-done`` (both modes): the router ledger's one-shot "a
top-level task finished" poke — exactly ONE owner message (WhatsApp/email via
the jarvis-phone drainer, NEVER an event/call), deduped on session_id in a
state file next to the store. Box mode forwards to the Mac, which does the
enqueue (the box never writes to jarvis-phone dirs).
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from common import config
from gateway import registry
from gateway.auth import require_bearer_any

router = APIRouter()

logger = logging.getLogger(__name__)

_STORE_NAME = "inbox.json"
_TASK_DONE_SEEN = "task-done-seen.json"
_PHONE_DIR_DEFAULT = "~/Desktop/personal/jarvis-phone"

# Inbox item ids are example message IDs as stored by scripts/vendor_watch.py
# (`h["ID"]`): url-safe alphanumerics plus `-` and `_`. Anything else can only
# be a probe — reject it before it is ever interpolated into a forwarded URL
# (`notify?`/`notify#` pivot onto other routes, `..` climbs the path).
_ITEM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _store_dir() -> Path:
    return Path(
        os.environ.get("JARVIS_VENDOR_WATCH_HOME", "~/.jarvis/vendor-watch")
    ).expanduser()


def _parse_items(raw: str | None) -> list[dict]:
    """Store text -> item list; anything malformed or absent reads empty."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _load_items() -> list[dict]:
    """Read the store under a shared lock (missing file -> empty list)."""
    directory = _store_dir()
    lock_path = directory / f"{_STORE_NAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            try:
                raw = (directory / _STORE_NAME).read_text(encoding="utf-8")
            except FileNotFoundError:
                return []
            return _parse_items(raw)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _mark_read(item_id: str) -> bool:
    """Flip `read` to true persistently under an exclusive lock. False = unknown id."""
    directory = _store_dir()
    lock_path = directory / f"{_STORE_NAME}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            path = directory / _STORE_NAME
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = None
            items = _parse_items(raw)
            if not any(item.get("id") == item_id for item in items):
                return False
            for item in items:
                if item.get("id") == item_id:
                    item["read"] = True
            tmp = directory / f"{_STORE_NAME}.tmp.{os.getpid()}"
            tmp.write_text(
                json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _task_done_seen(session_id: str) -> bool:
    """True if this session already produced its one task-done message."""
    try:
        data = json.loads((_store_dir() / _TASK_DONE_SEEN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and session_id in data


def _task_done_record(session_id: str) -> None:
    """Persist session_id -> now under the same fcntl + atomic-write idiom
    as _mark_read. Callers record ONLY after the enqueue/forward succeeded,
    so a failed attempt stays retryable."""
    directory = _store_dir()
    lock_path = directory / f"{_TASK_DONE_SEEN}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            path = directory / _TASK_DONE_SEEN
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = None
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data[session_id] = _now_iso()
            tmp = directory / f"{_TASK_DONE_SEEN}.tmp.{os.getpid()}"
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _newest_first(items: list[dict]) -> list[dict]:
    """ISO8601Z strings sort lexicographically; received_at falls back to created_at."""

    def key(item: dict) -> str:
        return str(item.get("received_at") or item.get("created_at") or "")

    return sorted(items, key=key, reverse=True)


def _unread_count(items: list[dict]) -> int:
    return sum(1 for item in items if not item.get("read"))


def _phone_dir() -> Path | None:
    """jarvis-phone repo root, or None = queue writes disabled.

    JARVIS_PHONE_DIR overrides (tests point it at a tmp dir). Without an
    override, tests (pytest sets PYTEST_CURRENT_TEST) get None so a notify
    can NEVER drop a file into the real outbound queue — J2's regression
    posts new>0 and would otherwise trigger a real WhatsApp send.
    """
    override = os.environ.get("JARVIS_PHONE_DIR")
    if override:
        return Path(override).expanduser()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return Path(_PHONE_DIR_DEFAULT).expanduser()


def _now_iso() -> str:
    """Naive local isoformat — same as jarvis-phone's writers (watcher._iso).
    The drainer's _is_stale does fromisoformat(created) - naive now(); a "Z"
    suffix parses aware on py≥3.11 and the subtraction raises TypeError."""
    return datetime.now().isoformat(timespec="seconds")


def _queue_file() -> str:
    """jarvis-phone queue filename convention: <ts>-<pid>.json."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.json"


def _enqueue(path: Path, payload: dict) -> bool:
    """Atomic write (tmp + rename, same idiom as _mark_read). False = IO error."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("inbox: jarvis-phone enqueue failed (%s): %s", path, exc)
        return False


def _owner_contact(directory: Path) -> tuple[str, str]:
    """(phone_whatsapp, email) from jarvis-phone config/owner.json; empty on any failure."""
    try:
        data = json.loads(
            (directory / "config" / "owner.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("phone_whatsapp") or ""), str(data.get("email") or "")


def _unread_vendors(items: list[dict], count: int) -> list[str]:
    """Distinct vendors of the newest `count` unread items, newest-first."""
    vendors: list[str] = []
    for item in _newest_first([i for i in items if not i.get("read")]):
        vendor = str(item.get("vendor") or item.get("sender") or "").strip()
        if vendor and vendor not in vendors:
            vendors.append(vendor)
    return vendors[:count]


def _enqueue_owner_text(text: str, subject: str) -> bool:
    """One owner message via the jarvis-phone drainer: WhatsApp normally,
    email while the overnight marker is set. False = not enqueued (no phone
    dir, no owner contact, or IO error) — callers treat that as retryable."""
    directory = _phone_dir()
    if directory is None:
        return False
    phone, email = _owner_contact(directory)
    if (directory / "queue" / "OVERNIGHT").exists() and email:
        payload = {
            "channel": "email",
            "created": _now_iso(),
            "payload": {"to": email, "subject": subject, "body": text},
        }
    elif phone:
        payload = {
            "channel": "whatsapp",
            "created": _now_iso(),
            "payload": {"to": phone, "text": text},
        }
    else:
        logger.warning("inbox: no owner contact (owner.json), message skipped")
        return False
    return _enqueue(directory / "queue" / "outbound" / _queue_file(), payload)


def _enqueue_outbound(new: int, items: list[dict]) -> None:
    """One guaranteed-path message: WhatsApp normally, email while overnight."""
    if new <= 0:
        return
    text = f"{new} new vendor email(s)"
    vendors = ", ".join(_unread_vendors(items, new))
    if vendors:
        text += f": {vendors}"
    text += ". Open jarvis://inbox"
    subject = f"[jarvis] vendor inbox: {new} new"
    _enqueue_owner_text(text, subject)


def _enqueue_urgent_event(new: int, items: list[dict], body: dict) -> None:
    """Urgent mail ALSO raises a jarvis-phone event (owner may get called).

    Mirrors jarvis-event.sh's shape; call_eligible is true on this path and
    this path ONLY — non-urgent new mail is message-only, never a call.
    """
    directory = _phone_dir()
    if directory is None or new <= 0:
        return
    newest = _newest_first([i for i in items if not i.get("read")])
    item = newest[0] if newest else {}
    subject = str(body.get("subject") or item.get("subject") or "vendor inbox urgent item")
    summary = str(body.get("summary") or item.get("summary") or "")
    steps = body.get("next_steps", item.get("next_steps"))
    details = summary
    if isinstance(steps, list) and steps:
        details = (details + "\n" if details else "") + "\n".join(str(s) for s in steps)
    event = {
        "type": "decision-request",
        "kind": "needs_input",
        "project": "jarvis-companion",
        "task": "vendor inbox urgent item",
        "call_eligible": True,
        "summary": subject,
        "details": details,
        "created": _now_iso(),
        "source": {"origin_cwd": str(Path(__file__).resolve().parent.parent)},
    }
    _enqueue(directory / "queue" / "events" / _queue_file(), event)


def _enqueue_guaranteed(new: int, items: list[dict], body: dict) -> None:
    """Guaranteed-path side of notify; any failure is logged, never raised."""
    try:
        _enqueue_outbound(new, items)
        if body.get("urgent") is True:
            _enqueue_urgent_event(new, items, body)
    except Exception:
        logger.warning("inbox: guaranteed-path enqueue failed", exc_info=True)


# Split deploy: MAC_GATEWAY_URL set = this process runs on the box, the
# vendor-watch store lives on the Mac instead. Same env + helper shape as
# gateway/central.py and gateway/screen.py.
MAC_GATEWAY_ENV = "MAC_GATEWAY_URL"
MAC_PROXY_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


def _mac_gateway() -> str | None:
    return os.environ.get(MAC_GATEWAY_ENV, "").rstrip("/") or None


def _mac_client() -> httpx.AsyncClient:
    """Outbound client to the Mac gateway (tests swap in an ASGI transport here)."""
    return httpx.AsyncClient(timeout=MAC_PROXY_TIMEOUT)


def _ios_token() -> str | None:
    return config.get_token(name="ios_token")


async def _mac_get_inbox() -> dict | None:
    """GET the Mac gateway's own /inbox; None on any failure (down, no token, bad body)."""
    token = _ios_token()
    if not token:
        return None
    try:
        async with _mac_client() as client:
            resp = await client.get(
                f"{_mac_gateway()}/inbox", headers={"Authorization": f"Bearer {token}"}
            )
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("mac gateway /inbox failed: %s", type(exc).__name__)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    return data


async def _mac_mark_read(item_id: str) -> httpx.Response | None:
    """POST the Mac gateway's own mark-read route; None = unreachable/no token."""
    token = _ios_token()
    if not token:
        return None
    try:
        async with _mac_client() as client:
            return await client.post(
                f"{_mac_gateway()}/inbox/{urllib.parse.quote(item_id, safe='')}/read",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        logger.info("mac gateway /inbox/%s/read failed: %s", item_id, type(exc).__name__)
        return None


async def _mac_task_done(session_id: str, purpose: str) -> httpx.Response | None:
    """POST the Mac gateway's own task-done route; None = unreachable/no token."""
    token = _ios_token()
    if not token:
        return None
    try:
        async with _mac_client() as client:
            return await client.post(
                f"{_mac_gateway()}/inbox/task-done",
                headers={"Authorization": f"Bearer {token}"},
                json={"session_id": session_id, "purpose": purpose},
            )
    except httpx.HTTPError as exc:
        logger.info("mac gateway /inbox/task-done failed: %s", type(exc).__name__)
        return None


# Tailscale's CGNAT range (https://tailscale.com/kb/1015/100.x-addresses) —
# every tailnet node gets one of these, and this port is never bound to
# anything but 127.0.0.1 (tailscale serve fronts it on the tailnet side), so
# a caller presenting one of these addresses already had to be an
# authenticated tailnet peer to reach this process at all.
_TAILNET_CGNAT = ipaddress.ip_network("<cgnat-ip>/10")


def _loopback_guard(request: Request) -> None:
    """/inbox/notify: localhost always gets through; a tailnet peer ONLY in
    box mode (MAC_GATEWAY_URL set). Anything else 403s.

    Confirmed empirically (2026-10-16): tailscale serve forwards the REAL
    remote tailnet address here, not 127.0.0.1 — so this is what lets the
    Mac's mail watcher reach the box's copy of this route cross-host over
    the tailnet. The tailnet allowance is scoped to box mode ONLY: on the
    Mac this route still runs the guaranteed WhatsApp/email enqueue (and,
    with ``urgent: true``, a call_eligible event) — relaxing the guard
    there would let any tailnet peer holding the shared token (which is
    more widely held than ios_token: the PC agent config, the iCloud
    Shortcut) trigger a real message or phone call to the owner. The
    bearer-token check (Depends above) is still the real access control
    either way; this guard is the extra "never the public internet" belt,
    and in Mac mode it stays as strict as it always was.
    """
    host = request.client.host if request.client else None
    if host == "127.0.0.1":
        return
    if _mac_gateway():  # box mode only — see docstring
        try:
            if host and ipaddress.ip_address(host) in _TAILNET_CGNAT:
                return
        except ValueError:
            pass
    raise HTTPException(status_code=403, detail="loopback only")


@router.get("/inbox", dependencies=[Depends(require_bearer_any("ios_token"))])
async def get_inbox() -> dict:
    if _mac_gateway():
        data = await _mac_get_inbox()
        return data if data is not None else {"items": [], "unread": 0}
    items = _newest_first(_load_items())
    return {"items": items, "unread": _unread_count(items)}


@router.post(
    "/inbox/{item_id}/read", dependencies=[Depends(require_bearer_any("ios_token"))]
)
async def mark_read(item_id: str) -> dict:
    # Store ids are always regex-clean (see _ITEM_ID_RE), so this 404s local
    # lookups no differently — but in box mode it fires BEFORE the forward, so
    # a probe like /inbox/notify%3F/read can never pivot onto another Mac route.
    if not _ITEM_ID_RE.fullmatch(item_id):
        raise HTTPException(status_code=404, detail="unknown inbox item")
    if _mac_gateway():
        resp = await _mac_mark_read(item_id)
        if resp is None:
            raise HTTPException(status_code=503, detail="mac offline")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="unknown inbox item")
        try:
            return resp.json()
        except ValueError:
            raise HTTPException(status_code=503, detail="mac offline") from None
    if not _mark_read(item_id):
        raise HTTPException(status_code=404, detail="unknown inbox item")
    items = _load_items()
    return {"ok": True, "unread": _unread_count(items)}


class TaskDoneBody(BaseModel):
    session_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)


@router.post(
    "/inbox/task-done",
    dependencies=[Depends(require_bearer_any("ios_token", "token"))],
)
async def task_done(body: TaskDoneBody) -> dict:
    """Router ledger: a top-level task reached DONE — tell the owner ONCE.

    Deduped on session_id (state file next to the store); the id is recorded
    only after the enqueue/forward succeeds, so a failed attempt stays
    retryable — router retries and re-transitions produce exactly one
    message. No loopback guard (same posture as mark_read: the box's forward
    arrives cross-host over the tailnet; the bearer check is the access
    control). This route NEVER enqueues a jarvis-phone event (call) —
    WhatsApp/email message only, via the existing drainer. In box mode the
    POST forwards to the Mac, which does the enqueue (the box never writes
    to jarvis-phone dirs); Mac unreachable/non-2xx -> 503, not recorded.
    """
    session_id = body.session_id
    purpose = body.purpose[:200]
    if _task_done_seen(session_id):
        return {"ok": True, "notified": False}
    if _mac_gateway():
        resp = await _mac_task_done(session_id, purpose)
        if resp is None or resp.status_code >= 400:
            raise HTTPException(status_code=503, detail="mac offline")
        _task_done_record(session_id)
        try:
            return resp.json()
        except ValueError:
            return {"ok": True, "notified": True}
    if not _enqueue_owner_text(
        f"✅ {purpose} is done", f"[jarvis] task done: {purpose}"
    ):
        raise HTTPException(status_code=503, detail="enqueue failed")
    _task_done_record(session_id)
    return {"ok": True, "notified": True}


# /inbox/notify auth: box mode is ios_token-ONLY — the shared token is more
# widely held (PC agent config, the iCloud Shortcut) and must not reach the
# box's copy of this route. Mac mode keeps accepting either token: J1's
# watcher holds the shared token and the Mac's notify also runs the
# guaranteed-path enqueue.
_NOTIFY_AUTH_BOX = require_bearer_any("ios_token")
_NOTIFY_AUTH_ANY = require_bearer_any("ios_token", "token")


async def _notify_auth(authorization: str | None = Header(default=None)) -> None:
    await (_NOTIFY_AUTH_BOX if _mac_gateway() else _NOTIFY_AUTH_ANY)(authorization)


@router.post("/inbox/notify", dependencies=[Depends(_notify_auth)])
async def notify(request: Request) -> dict:
    """Watcher (J1/J3) pokes new mail: recount unread, push `inbox_new` to ios.

    Body ``{"new": int, "unread": int, "urgent": bool}`` (all optional) — the
    unread count in the pushed payload is always recounted (from the local
    store, or from the Mac's own /inbox in box mode — see module docstring),
    never trusted from the body. In box mode the route takes the ios_token
    only (see _notify_auth) and clamps ``new`` to 0..1000. ``new > 0`` also
    fires the guaranteed path
    (jarvis-phone WhatsApp/email + urgent event) — but ONLY in Mac mode; in
    box mode the Mac's own local notify call already ran that, so running it
    again here would double-send. Enqueue failures never fail the request.
    No ios socket connected (or send failure) still replies 200
    ``{"ok": true, "delivered": false}`` — notify must never raise.
    """
    _loopback_guard(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    new = body.get("new")
    if _mac_gateway():
        if isinstance(new, int):
            new = max(0, min(1000, new))  # clamp the counter relayed to ios
        mac_data = await _mac_get_inbox()
        unread = mac_data.get("unread", 0) if isinstance(mac_data, dict) else 0
    else:
        items = _load_items()
        unread = _unread_count(items)
        _enqueue_guaranteed(new if isinstance(new, int) else 0, items, body)
    payload: dict = {"unread": unread}
    if isinstance(new, int):
        payload["new"] = new
    try:
        delivered = await registry.send_to_role(
            "ios",
            {
                "id": f"inbox-{uuid.uuid4().hex[:8]}",
                "type": "inbox_new",
                "payload": payload,
            },
        )
    except Exception:
        delivered = False
    return {"ok": True, "delivered": bool(delivered)}


def _cli(argv: list[str]) -> int:
    """``python3 -m gateway.inbox --test``: ONE real WhatsApp-to-self message
    ("[TEST] jarvis inbox notify path OK — jarvis://inbox") into the real
    outbound queue, so the hub can verify the whole chain end-to-end."""
    if "--test" not in argv:
        print(__name__ + ": pass --test to enqueue one test WhatsApp message")
        return 2
    directory = Path(os.environ.get("JARVIS_PHONE_DIR") or _PHONE_DIR_DEFAULT).expanduser()
    phone, _ = _owner_contact(directory)
    if not phone:
        print("no phone_whatsapp in " + str(directory / "config" / "owner.json"), file=sys.stderr)
        return 1
    payload = {
        "channel": "whatsapp",
        "created": _now_iso(),
        "payload": {"to": phone, "text": "[TEST] jarvis inbox notify path OK — jarvis://inbox"},
    }
    path = directory / "queue" / "outbound" / _queue_file()
    if not _enqueue(path, payload):
        print(f"enqueue failed: {path}", file=sys.stderr)
        return 1
    print(f"queued {path.name} (whatsapp, to owner phone per owner.json)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
