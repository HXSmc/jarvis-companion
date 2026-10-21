"""Session cards: what a router session declares for its card on the phone board.

A session on the box calls `jarvis-card` (scripts/jarvis-card), which posts here
with the shared gateway token (same door as `jarvis-hw-request`):

  POST /cards/event   {"session", "action", ...}   doing | ask | clear | delegate | link
  POST /cards/file    raw body + ?session=&name=&title=   a report/plan/screenshot for the card

`session` may be the router session id or the Claude session uuid
($CLAUDE_CODE_SESSION_ID); `resolve_session` maps the uuid through a read-only
look at the router's sqlite file (ROUTER_DB). The same read-only look gives the
plain-words pass the session's real latest status lines and its full last
reply (`recent_events`, `last_reply_at`): the router's own `summary` is only
the first 240 characters of the last reply, which is how stale lines such as
"Now I'll update REPORT.md" reached the Recap.

Everything lives in one private JSON file (CARDS_PATH) plus card files under
CARD_FILES_DIR/<session>/<id>.<ext>. central.py adds `card_for()` to every
/central/sessions row and serves the phone's file + answer routes, since it
already owns the router message path.

A new ask calls every NEED_LISTENERS callback with (session_id, need) so the
notifications code can push "Needs you" without polling.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from gateway.auth import require_bearer

logger = logging.getLogger(__name__)

ID_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
_ID_RE = re.compile(ID_PATTERN)
FILE_ID_PATTERN = r"^[0-9a-f]{32}$"
_FILE_ID_RE = re.compile(FILE_ID_PATTERN)

NEED_KINDS = ("approve", "choice", "yesno", "checklist", "preview", "info")
MAX_OPTIONS = 6
MAX_ITEMS = 12
MAX_FILES = 8  # per session; the oldest goes first
MAX_LINKS = 6
MAX_DELEGATIONS = 12
MAX_FILE_BYTES = 10 * 1024 * 1024
DOING_TTL = timedelta(minutes=15)  # a declared "doing" line older than this gives way to the model's
ANSWERED_SHOW = timedelta(hours=6)  # an answered ask stays on the card as "You answered: ..."

# Card file types the app can show: markdown/text rendered, PDF in PDFKit, images inline.
FILE_TYPES = {
    "text/markdown": (".md", "markdown"),
    "text/plain": (".txt", "text"),
    "application/pdf": (".pdf", "pdf"),
    "image/png": (".png", "image"),
    "image/jpeg": (".jpg", "image"),
}
_EXT_TYPES = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
              ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

# Status lines the router writes that say nothing to the owner.
_GENERIC_STATUS = re.compile(r"^(working|ledger: .*|sweep: .*|session exited.*|idle|done|resumed.*|queued.*)$", re.I)

NEED_LISTENERS: list = []  # callables (session_id: str, need: dict) -> None


def cards_path() -> Path:
    return Path(os.environ.get("JARVIS_CARDS_PATH", "~/.jarvis/cards.json")).expanduser()


def files_dir() -> Path:
    return Path(os.environ.get("JARVIS_CARD_FILES_DIR", "~/.jarvis/card_files")).expanduser()


def router_db() -> Path:
    return Path(os.environ.get("MINIPC_ROUTER_DB", "~/minipc-router/router.db")).expanduser()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _line(value, limit: int) -> str:
    """One clean line: control characters dropped, spaces folded, capped."""
    if not isinstance(value, str):
        return ""
    text = " ".join(re.sub(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]", " ", value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- store ---------------------------------------------------------------------

def load() -> dict:
    try:
        data = json.loads(cards_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    """Atomic private write (dir 0o700, file 0o600). Raises OSError."""
    path = cards_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _update(sid: str, change) -> dict:
    """Load, apply change(entry) to this session's entry, save; returns the entry."""
    data = load()
    entry = data.get(sid) if isinstance(data.get(sid), dict) else {}
    change(entry)
    data[sid] = entry
    try:
        save(data)
    except OSError as exc:
        logger.warning("cards save failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="could not save") from None
    return entry


# --- router sqlite (read-only) -----------------------------------------------

def _db():
    path = router_db()
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, args: tuple) -> list[sqlite3.Row]:
    conn = _db()
    if conn is None:
        return []
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        logger.info("router db read failed: %s", type(exc).__name__)
        return []
    finally:
        conn.close()


def resolve_session(raw: str) -> str | None:
    """Router id or Claude session uuid -> router session id; None when unknown.

    Without the router file (a Mac-only deploy) the raw id is trusted as is.
    """
    if not isinstance(raw, str) or not _ID_RE.fullmatch(raw):
        return None
    if not router_db().is_file():
        return raw
    rows = _query("SELECT id FROM sessions WHERE id=? OR claude_session_uuid=? "
                  "ORDER BY created_at DESC LIMIT 1", (raw, raw))
    return rows[0]["id"] if rows else None


def recent_events(sid: str, limit: int = 60) -> dict:
    """{"statuses": [newest first, distinct, meaningful], "reply": full last text, "reply_at", "reply_seq"}.

    Empty dict without the router file. `reply` is the router's last text event
    (its tail, up to 4000 characters), so a final report is read whole.
    """
    rows = _query("SELECT seq, type, text, ts FROM events WHERE session_id=? "
                  "ORDER BY seq DESC LIMIT ?", (sid, limit))
    if not rows:
        return {}
    statuses: list[str] = []
    for r in rows:
        if r["type"] != "status" or not r["text"]:
            continue
        text = _line(r["text"], 160)
        if not text or _GENERIC_STATUS.match(text) or text.startswith("Running ") or text in statuses:
            continue
        statuses.append(text)
        if len(statuses) >= 5:
            break
    reply = _query("SELECT seq, text, ts FROM events WHERE session_id=? AND type='text' "
                   "ORDER BY seq DESC LIMIT 1", (sid,))
    out = {"statuses": statuses, "reply": "", "reply_at": None, "reply_seq": None}
    if reply:
        out.update(reply=reply[0]["text"] or "", reply_at=reply[0]["ts"], reply_seq=reply[0]["seq"])
    return out


def last_reply_at(sid: str) -> datetime | None:
    rows = _query("SELECT ts FROM events WHERE session_id=? AND type IN ('text','needs_input') "
                  "ORDER BY seq DESC LIMIT 1", (sid,))
    return _parse(rows[0]["ts"]) if rows else None


# --- session-facing routes (shared token) --------------------------------------

session_router = APIRouter(dependencies=[Depends(require_bearer)])


class CardEvent(BaseModel):
    session: str = Field(pattern=ID_PATTERN)
    action: str = Field(pattern=r"^(doing|ask|clear|delegate|link)$")
    text: str = Field(default="", max_length=600)
    kind: str = Field(default="approve", pattern=r"^[a-z]{1,16}$")
    options: list[str] = Field(default_factory=list, max_length=MAX_OPTIONS)
    items: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    file_id: str = Field(default="", max_length=32)
    url: str = Field(default="", max_length=600)
    child: str = Field(default="", max_length=64)


def _session_or_404(raw: str) -> str:
    sid = resolve_session(raw)
    if not sid:
        raise HTTPException(status_code=404, detail="unknown session")
    return sid


def _notify(sid: str, need: dict) -> None:
    for listener in list(NEED_LISTENERS):
        try:
            listener(sid, dict(need))
        except Exception as exc:  # a notifier bug never fails the session's call
            logger.info("need listener failed: %s", type(exc).__name__)


@session_router.post("/cards/event")
async def card_event(ev: CardEvent) -> dict:
    sid = _session_or_404(ev.session)
    now = _iso(_now())
    text = _line(ev.text, 600)

    if ev.action == "doing":
        if not text:
            raise HTTPException(status_code=422, detail="text required")
        _update(sid, lambda e: e.__setitem__("doing", {"text": _line(text, 100), "at": now}))
        return {"ok": True, "session_id": sid}

    if ev.action == "clear":
        _update(sid, lambda e: e.pop("need", None))
        return {"ok": True, "session_id": sid}

    if ev.action == "link":
        if not re.fullmatch(r"https://[^\s<>\"']{4,590}", ev.url):
            raise HTTPException(status_code=422, detail="url must be https")

        def add_link(e):
            links = [l for l in e.get("links", []) if l.get("url") != ev.url]
            links.append({"url": ev.url, "title": _line(text, 80) or ev.url[:80], "at": now})
            e["links"] = links[-MAX_LINKS:]
        _update(sid, add_link)
        return {"ok": True, "session_id": sid}

    if ev.action == "delegate":
        child = resolve_session(ev.child)
        if not child or child == sid:
            raise HTTPException(status_code=404, detail="unknown child session")

        def add_child(e):
            rows = [d for d in e.get("delegations", []) if d.get("child") != child]
            rows.append({"child": child, "what": _line(text, 120), "at": now})
            e["delegations"] = rows[-MAX_DELEGATIONS:]
        _update(sid, add_child)
        return {"ok": True, "session_id": sid, "child": child}

    # ask
    if ev.kind not in NEED_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {', '.join(NEED_KINDS)}")
    if not text:
        raise HTTPException(status_code=422, detail="text required")
    options = [o for o in (_line(o, 40) for o in ev.options) if o]
    items = [i for i in (_line(i, 120) for i in ev.items) if i]
    if ev.kind == "choice" and len(options) < 2:
        raise HTTPException(status_code=422, detail="choice needs 2 to 6 options")
    if ev.kind == "checklist" and not items:
        raise HTTPException(status_code=422, detail="checklist needs items")
    if ev.file_id and not _FILE_ID_RE.fullmatch(ev.file_id):
        raise HTTPException(status_code=422, detail="bad file id")
    need = {"id": uuid.uuid4().hex[:12], "kind": ev.kind, "question": text, "options": options,
            "items": items, "file_id": ev.file_id or None, "at": now, "answered": None}

    def set_need(e):
        if need["file_id"] and not any(f.get("id") == need["file_id"] for f in e.get("files", [])):
            raise HTTPException(status_code=404, detail="file not on this card")
        e["need"] = need
    _update(sid, set_need)
    _notify(sid, need)
    return {"ok": True, "session_id": sid, "need_id": need["id"]}


@session_router.post("/cards/file")
async def card_file(
    request: Request,
    session: str = Query(pattern=ID_PATTERN),
    name: str = Query(default="", max_length=200),
    title: str = Query(default="", max_length=200),
) -> dict:
    sid = _session_or_404(session)
    base = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if base not in FILE_TYPES:
        base = _EXT_TYPES.get(os.path.splitext(name.lower())[1], "")
    if base not in FILE_TYPES:
        raise HTTPException(status_code=415, detail="use .md, .txt, .pdf, .png or .jpg")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="file too large")
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    ext, view = FILE_TYPES[base]
    fid = uuid.uuid4().hex
    folder = files_dir() / sid
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(folder / f"{fid}{ext}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(data))
    except OSError as exc:
        logger.warning("card file save failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="could not save") from None
    clean_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", os.path.basename(name)).strip(". ")[:80] or f"file{ext}"
    row = {"id": fid, "name": clean_name, "title": _line(title, 80) or clean_name, "type": base,
           "view": view, "size": len(data), "at": _iso(_now())}

    def add_file(e):
        files = e.get("files", []) + [row]
        for old in files[:-MAX_FILES]:
            _remove_file(sid, old)
        e["files"] = files[-MAX_FILES:]
    _update(sid, add_file)
    return {"ok": True, "session_id": sid, "file_id": fid}


def _remove_file(sid: str, row: dict) -> None:
    ext = FILE_TYPES.get(row.get("type"), (".bin",))[0]
    if _FILE_ID_RE.fullmatch(str(row.get("id"))):
        try:
            (files_dir() / sid / f"{row['id']}{ext}").unlink()
        except OSError:
            pass


def file_path(sid: str, fid: str) -> tuple[Path, str] | None:
    """(path, media type) for a card file the store lists, else None."""
    if not _ID_RE.fullmatch(sid) or not _FILE_ID_RE.fullmatch(fid):
        return None
    entry = load().get(sid) or {}
    row = next((f for f in entry.get("files", []) if f.get("id") == fid), None)
    if not row or row.get("type") not in FILE_TYPES:
        return None
    path = files_dir() / sid / f"{fid}{FILE_TYPES[row['type']][0]}"
    if path.is_symlink() or not path.is_file():
        return None
    return path, row["type"]


# --- owner side: receipts, answers ---------------------------------------------

def record_message(sid: str, text: str, msg_id: str, status: str, action: str | None = None) -> None:
    """The owner's last message to a session; any reply also settles an open ask."""
    if not _ID_RE.fullmatch(sid or ""):
        return
    now = _iso(_now())

    def change(e):
        old = e.get("receipt") or {}
        at = old.get("at") if old.get("msg_id") == msg_id and old.get("at") else now
        e["receipt"] = {"text": _line(text, 200), "msg_id": msg_id, "at": at, "status": status,
                        "action": action}
        need = e.get("need")
        if status in ("received", "queued") and isinstance(need, dict) and not need.get("answered"):
            need["answered"] = {"answer": "reply", "text": _line(text, 200), "at": now}
    try:
        _update(sid, change)
    except HTTPException:
        pass  # a receipt is a nicety; the message itself already went out


def answer_text(need: dict, answer: str, note: str, checked: list[bool]) -> str:
    """The message the session gets when the owner taps a card button."""
    q = need.get("question", "")
    note = _line(note, 1000)
    if need.get("kind") == "checklist":
        marks = [f"{'[x]' if i < len(checked) and checked[i] else '[ ]'} {item}"
                 for i, item in enumerate(need.get("items", []))]
        body = "Owner checklist results for: " + q + "\n" + "\n".join(marks)
    else:
        body = f'Owner tapped "{answer}" on your card ask: {q}'
    if note:
        body += f"\nOwner note: {note}"
    return body


def valid_answer(need: dict, answer: str) -> bool:
    kind = need.get("kind")
    allowed = {
        "approve": ("Approve",), "preview": ("Looks good",), "yesno": ("Yes", "No"),
        "choice": tuple(need.get("options") or ()), "checklist": ("Send results",), "info": ("OK",),
    }.get(kind, ())
    return answer in allowed


def mark_answered(sid: str, need_id: str, answer: str) -> None:
    def change(e):
        need = e.get("need")
        if isinstance(need, dict) and need.get("id") == need_id:
            need["answered"] = {"answer": answer, "at": _iso(_now())}
    _update(sid, change)


# --- card assembly (called by central.sessions for every row) ------------------

def _receipt(entry: dict, session: dict) -> dict | None:
    r = entry.get("receipt")
    if not isinstance(r, dict) or not r.get("at"):
        return None
    sent = _parse(r["at"])
    status = r.get("status") or "received"
    if status in ("received", "queued") and sent:
        busy = session.get("health") in ("working", "quiet")
        replied = last_reply_at(session.get("session_id", ""))
        if busy:
            if status == "received":
                status = "working"
        elif replied and replied > sent:
            status = "answered"
    return {"text": r.get("text", ""), "at": r["at"], "status": status}


def card_for(session: dict, by_id: dict[str, dict], entries: dict | None = None) -> dict:
    """The `card` object for one /central/sessions row (always present, may be empty)."""
    sid = session.get("session_id") or ""
    entries = load() if entries is None else entries
    entry = entries.get(sid) if isinstance(entries.get(sid), dict) else {}
    now = _now()

    need = entry.get("need") if isinstance(entry.get("need"), dict) else None
    if need and need.get("answered"):
        at = _parse(need["answered"].get("at"))
        if not at or now - at > ANSWERED_SHOW:
            need = None
    children: list[dict] = []
    seen: set[str] = set()
    for d in entry.get("delegations", []):
        child = d.get("child")
        if child in seen:
            continue
        seen.add(child)
        children.append({"session_id": child, "what": d.get("what") or "", "declared": True})
    for other in by_id.values():
        osid = other.get("session_id")
        if other.get("parent_session_id") == sid and osid not in seen:
            seen.add(osid)
            children.append({"session_id": osid, "what": "", "declared": False})
    for c in children:
        row = by_id.get(c["session_id"]) or {}
        c.update(title=row.get("doing") or row.get("title") or "", phase=row.get("phase"),
                 health=row.get("health"), on_board=bool(row))

    parent = None
    psid = session.get("parent_session_id")
    if not psid:
        psid = next((osid for osid, e in entries.items() if isinstance(e, dict) and osid != sid
                     and any(d.get("child") == sid for d in e.get("delegations", []))), None)
    if psid:
        prow = by_id.get(psid) or {}
        parent = {"session_id": psid, "title": prow.get("doing") or prow.get("title") or "", "on_board": bool(prow)}

    doing = entry.get("doing") if isinstance(entry.get("doing"), dict) else None
    if doing:
        at = _parse(doing.get("at"))
        if session.get("phase") == "done" or not at or now - at > DOING_TTL:
            doing = None

    return {
        "need": need,
        "files": [{k: f.get(k) for k in ("id", "name", "title", "type", "view", "size", "at")}
                  for f in entry.get("files", [])],
        "links": entry.get("links", []),
        "children": children,
        "parent": parent,
        "receipt": _receipt(entry, session),
        "doing": doing.get("text") if doing else None,
    }
