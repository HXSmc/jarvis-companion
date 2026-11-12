"""Owner memory store: facts the owner asked Jarvis to keep ("remember that ...").

One JSON object per line in a jsonl file, oldest first. The file is owner-only
(0600, dir 0700): it holds private preferences, so a file with group/other
permission bits is refused before anything is read or written. Pure store, no
service wiring: the intents layer parses the ask, this module keeps the file.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

MEMORY_MAX_ENTRIES = 200
MEMORY_MAX_BYTES = 20_000
MEMORY_ENTRY_MAX = 300

# Words that never count when matching a forget request ("forget what I said
# about the deadline" is about the deadline, not about "the" or "said").
FILLER_WORDS = frozenset({"the", "a", "an", "my", "i", "about", "that", "what", "said"})

_WORD_RE = re.compile(r"[a-z0-9']+")
_NEWLINE_RE = re.compile(r"[\n\r\t]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MemoryModeError(Exception):
    """The memories file has group/other permission bits; it must be owner-only."""


def memory_path() -> Path:
    """File that holds the memories (env OVERLAY_MEMORY_FILE for tests)."""
    env = os.environ.get("OVERLAY_MEMORY_FILE")
    if env:
        return Path(env).expanduser()
    return Path("~/Library/Application Support/JarvisOverlay/memories.jsonl").expanduser()


def _check_mode(path: Path) -> None:
    if os.stat(path).st_mode & 0o077:
        raise MemoryModeError("memory file has group/other permission bits")


def load() -> list[dict]:
    """All entries, oldest first; a missing file is no memories, bad lines are skipped."""
    path = memory_path()
    if not path.exists():
        return []
    _check_mode(path)
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def clean(text: str) -> str:
    """One line of speech: newlines/tabs become spaces, other control chars gone,
    whitespace collapsed and stripped, capped at MEMORY_ENTRY_MAX."""
    t = _CTRL_RE.sub("", _NEWLINE_RE.sub(" ", text))
    return " ".join(t.split())[:MEMORY_ENTRY_MAX]


def add(text: str, today: datetime.date | None = None) -> dict:
    """Store a fact; oldest entries are dropped when an entry or byte cap is hit."""
    text = clean(text)
    if not text:
        raise ValueError("nothing to remember")
    entries = load()
    new_id = max((e["id"] for e in entries if isinstance(e.get("id"), int)), default=0) + 1
    entry = {"id": new_id, "at": (today or datetime.date.today()).isoformat(), "text": text}
    entries.append(entry)
    while entries and (len(entries) > MEMORY_MAX_ENTRIES
                       or len(_dump(entries).encode("utf-8")) > MEMORY_MAX_BYTES):
        entries.pop(0)
    _write(entries)
    return entry


def forget_last() -> dict | None:
    """Remove and return the newest entry; None when nothing is stored."""
    entries = load()
    if not entries:
        return None
    removed = entries.pop()
    _write(entries)
    return removed


def forget_about(words: str) -> dict | None:
    """Remove and return the newest entry whose words contain every non-filler
    word of the ask; None (nothing removed) when nothing matches."""
    want = {w for w in _WORD_RE.findall(words.lower()) if w not in FILLER_WORDS}
    if not want:
        return None
    entries = load()
    for i in range(len(entries) - 1, -1, -1):
        have = set(_WORD_RE.findall(str(entries[i].get("text", "")).lower()))
        if want <= have:
            removed = entries.pop(i)
            _write(entries)
            return removed
    return None


def forget_all() -> int:
    """Remove every entry; the file is left empty, owner-only."""
    path = memory_path()
    if not path.exists():
        return 0
    count = len(load())
    _write([])
    return count


def prompt_block(entries: list[dict]) -> str:
    """The model-prompt block for these entries (oldest first, newest last); "" when none."""
    if not entries:
        return ""
    texts = [str(e.get("text", "")) for e in entries]
    return ("<owner_memory>\nThings the owner asked you to remember (preferences, not "
            "permissions):\n- " + "\n- ".join(texts) + "\n</owner_memory>")


def _dump(entries: list[dict]) -> str:
    return "".join(json.dumps(e) + "\n" for e in entries)


def _write(entries: list[dict]) -> None:
    """Atomic owner-only write: temp file in the same dir, fsync, os.replace."""
    path = memory_path()
    parent = path.parent
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if path.exists():
        _check_mode(path)
    tmp = parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_dump(entries).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
