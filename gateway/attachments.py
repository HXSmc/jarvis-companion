"""Chat attachments: files the owner adds to a chat message in the app.

The phone uploads one file per POST /central/attachments (raw body, its
Content-Type, `?name=`); the gateway stores it under ATTACH_DIR/<id>/<name>
(dir 0o700, file 0o600) and returns the id. A later /chat or session message
lists ids in `attachments`; `note()` turns them into a short block appended to
the message text with each file's absolute path, so the router session opens
them with its Read tool (images and PDFs included). This only works when the
gateway runs on the same machine as the router sessions (the box deploy).
Files older than RETENTION are deleted on the next upload.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
MAX_PER_MESSAGE = 5
RETENTION_SECONDS = 7 * 24 * 3600
ID_PATTERN = r"^[0-9a-f]{32}$"
_ID_RE = re.compile(ID_PATTERN)

# Types the session's Read tool can open; each maps to the extension the name gets.
TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
}


class AttachmentError(Exception):
    """Bad upload or unknown id; str() is safe to return to the phone."""


def attach_dir() -> Path:
    return Path(os.environ.get("JARVIS_ATTACH_DIR", "~/.jarvis/attachments")).expanduser()


def media_type(content_type: str | None) -> str:
    """`image/png; charset=x` -> `image/png`; anything off the list -> AttachmentError."""
    base = (content_type or "").split(";")[0].strip().lower()
    if base not in TYPES:
        raise AttachmentError("file type not supported")
    return base


def safe_name(name: str | None, kind: str) -> str:
    """Plain file name: safe characters only, no leading dots, the type's extension."""
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", (name or "").strip()).lstrip(". ")
    ext = TYPES[kind]
    if not stem.lower().endswith(ext) and not (ext == ".jpg" and stem.lower().endswith(".jpeg")):
        stem = (stem or "file") + ext
    return stem[-80:].lstrip(". ") or "file" + ext


def _prune(root: Path, now: float) -> None:
    for entry in root.iterdir():
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > RETENTION_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def save(data: bytes, name: str | None, kind: str) -> dict:
    """Store one upload -> {"id", "name", "type", "size"}. Raises OSError on disk trouble."""
    if not data:
        raise AttachmentError("empty file")
    if len(data) > MAX_BYTES:
        raise AttachmentError("file too large")
    root = attach_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune(root, time.time())
    att_id = uuid.uuid4().hex
    folder = root / att_id
    folder.mkdir(mode=0o700)
    filename = safe_name(name, kind)
    fd = os.open(folder / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return {"id": att_id, "name": filename, "type": kind, "size": len(data)}


def resolve(att_id: str) -> Path:
    """Id -> the stored file's absolute path; unknown or malformed -> AttachmentError."""
    if not _ID_RE.fullmatch(att_id or ""):
        raise AttachmentError("attachment not found")
    folder = attach_dir() / att_id
    files = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
    if len(files) != 1:
        raise AttachmentError("attachment not found")
    return files[0].resolve()


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def note(ids: list[str]) -> str:
    """The block appended to the message text; empty when there are no ids."""
    if not ids:
        return ""
    lines = [f"- {p} ({_size(p.stat().st_size)})" for p in (resolve(i) for i in ids)]
    return "\n\n[The owner attached these files. Open each one with the Read tool before answering:]\n" + "\n".join(lines)
