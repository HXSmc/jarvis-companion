"""Inbox endpoints: serve J1's vendor-watch store read-only + mark-read + notify.

The store (``~/.jarvis/vendor-watch/inbox.json``, dir overridable via
``JARVIS_VENDOR_WATCH_HOME``) is written by J1's watcher; this module NEVER
writes items, NEVER summarises (summaries/next_steps are generated once at
ingest by J1) — it only lists, flips the read flag, and pushes an
``inbox_new`` command to the connected iOS socket. File access is serialised
with an fcntl lock on ``inbox.json.lock``; writes are atomic (tmp + rename).
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from gateway import registry
from gateway.auth import require_bearer_any

router = APIRouter()

_STORE_NAME = "inbox.json"


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


def _newest_first(items: list[dict]) -> list[dict]:
    """ISO8601Z strings sort lexicographically; received_at falls back to created_at."""

    def key(item: dict) -> str:
        return str(item.get("received_at") or item.get("created_at") or "")

    return sorted(items, key=key, reverse=True)


def _unread_count(items: list[dict]) -> int:
    return sum(1 for item in items if not item.get("read"))


def _loopback_guard(request: Request) -> None:
    """/inbox/notify is a localhost-only trigger: anything but 127.0.0.1 gets 403."""
    host = request.client.host if request.client else None
    if host != "127.0.0.1":
        raise HTTPException(status_code=403, detail="loopback only")


@router.get("/inbox", dependencies=[Depends(require_bearer_any("ios_token"))])
async def get_inbox() -> dict:
    items = _newest_first(_load_items())
    return {"items": items, "unread": _unread_count(items)}


@router.post(
    "/inbox/{item_id}/read", dependencies=[Depends(require_bearer_any("ios_token"))]
)
async def mark_read(item_id: str) -> dict:
    if not _mark_read(item_id):
        raise HTTPException(status_code=404, detail="unknown inbox item")
    items = _load_items()
    return {"ok": True, "unread": _unread_count(items)}


@router.post(
    "/inbox/notify", dependencies=[Depends(require_bearer_any("ios_token", "token"))]
)
async def notify(request: Request) -> dict:
    """Local watcher (J1/J3) pokes new mail: recount unread, push `inbox_new` to ios.

    Body ``{"new": int, "unread": int}`` (both optional) — the unread count in
    the pushed payload is always recounted from the store, never trusted from
    the body. No ios socket connected (or send failure) still replies 200
    ``{"ok": true, "delivered": false}``` — notify must never raise.
    """
    _loopback_guard(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    new = body.get("new")
    unread = _unread_count(_load_items())
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
