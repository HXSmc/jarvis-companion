"""Saved workspaces: named snapshots of the overlay's cards and open pages.

One JSON file per workspace in the overlay support dir, owner-only (0600, dir
0700). Names come from speech, so they are normalised to words and the file
name is always a strict slug — never a raw spoken string as a path. Pure
store: the service validates the cards (hud.validate_card) before saving.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.parse
from pathlib import Path

WORKSPACE_MAX_COUNT = 30
WORKSPACE_MAX_BYTES = 200_000
WORKSPACE_MAX_CARDS = 20
WORKSPACE_MAX_PAGES = 12
# Words dropped when turning a spoken name into slug/match words ("our battery
# research" is about the battery).
OPTIONAL_WORDS = ("our", "my", "the", "research", "workspace")

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_MAX_URL = 2000


class WorkspaceError(Exception):
    """A workspace save/load failed; `reason` says which: full | name | mode | io."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def workspace_dir() -> Path:
    """Dir that holds the saved workspaces (env OVERLAY_WORKSPACE_DIR for tests)."""
    env = os.environ.get("OVERLAY_WORKSPACE_DIR")
    if env:
        return Path(env).expanduser()
    return Path("~/Library/Application Support/JarvisOverlay/workspaces").expanduser()


def name_words(name: str) -> list[str]:
    """Lower-case word tokens of a spoken name, optional words removed."""
    return [w for w in _NON_WORD_RE.sub(" ", name.lower()).split() if w not in OPTIONAL_WORDS]


def slug(name: str) -> str:
    """Safe file stem for the name, "" when the words make no valid slug."""
    s = "-".join(name_words(name))[:40].strip("-")
    return s if _SLUG_RE.fullmatch(s) else ""


def display_name(name: str) -> str:
    """Name as saved and shown: lower case, one space between words, no leading
    our/my/the, capped at 60 chars."""
    words = " ".join(name.lower().split()).split()
    while words and words[0] in ("our", "my", "the"):
        words.pop(0)
    return " ".join(words)[:60]


def safe_page(url: object) -> bool:
    """True only for a plain http/https URL that is safe to keep and reopen."""
    if not isinstance(url, str) or len(url) > _MAX_URL:
        return False
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def list_all() -> list[dict]:
    """Every readable workspace: {slug, name, saved_at, cards, pages}, by name."""
    d = workspace_dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        out.append({"slug": doc.get("slug") or f.stem,
                    "name": str(doc.get("name", "")),
                    "saved_at": str(doc.get("saved_at", "")),
                    "cards": len(doc.get("cards") or []),
                    "pages": len(doc.get("pages") or [])})
    out.sort(key=lambda item: item["name"])
    return out


def match(name: str) -> list[str]:
    """Slugs the spoken name may mean: a workspace whose own name spells the same
    words as the spoken name (our/my/the don't count; research/workspace do) wins
    alone, else every workspace whose words contain all the spoken words
    (optional words never count on the spoken side)."""
    want = name_words(name)
    if not want:
        return []
    items = list_all()
    spoken = _match_words(name)
    if spoken:
        for item in items:
            if spoken == _match_words(item["name"]):
                return [item["slug"]]
    return [item["slug"] for item in items
            if set(want) <= set(name_words(item["name"]))]


def _match_words(name: str) -> list[str]:
    """Words of a name with only the leading articles (our/my/the) dropped."""
    return [w for w in _NON_WORD_RE.sub(" ", name.lower()).split() if w not in ("our", "my", "the")]


def save(name: str, cards: list[dict], pages: list[dict], enlarged: str | None,
         now: datetime.datetime | None = None) -> dict:
    """Write <slug>.json and return the saved doc. Non-dict cards are dropped,
    pages are kept only when their URL is a plain http/https URL, `enlarged`
    survives only when it names a kept card."""
    s = slug(name)
    if not s:
        raise WorkspaceError("name")
    d = workspace_dir()
    if not exists(s):
        count = len(list(d.glob("*.json"))) if d.is_dir() else 0
        if count >= WORKSPACE_MAX_COUNT:
            raise WorkspaceError("full")
    kept_cards = [c for c in cards if isinstance(c, dict)][:WORKSPACE_MAX_CARDS]
    kept_pages: list[dict] = []
    for p in pages:
        url = p.get("url") if isinstance(p, dict) else None
        if url and safe_page(url):
            kept_pages.append({"url": url, "title": str(p.get("title", ""))[:120]})
        if len(kept_pages) == WORKSPACE_MAX_PAGES:
            break
    ids = {c.get("id") for c in kept_cards}
    when = now or datetime.datetime.now(datetime.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    doc = {"name": display_name(name), "slug": s,
           "saved_at": when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "cards": kept_cards, "pages": kept_pages,
           "enlarged": enlarged if enlarged in ids else None}
    body = json.dumps(doc, ensure_ascii=False)
    while doc["cards"] and len(body.encode("utf-8")) > WORKSPACE_MAX_BYTES:
        doc["cards"].pop()
        body = json.dumps(doc, ensure_ascii=False)
    _write(d, s, body)
    return doc


def load(s: str) -> dict | None:
    """The saved doc for the slug; None when the slug is invalid or unknown."""
    if not _SLUG_RE.fullmatch(s):
        return None
    path = workspace_dir() / f"{s}.json"
    if not path.exists():
        return None
    _check_mode(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceError("io") from exc
    return doc if isinstance(doc, dict) else None


def exists(s: str) -> bool:
    return bool(_SLUG_RE.fullmatch(s)) and (workspace_dir() / f"{s}.json").exists()


def delete(s: str) -> bool:
    """Remove the workspace file; True when it existed."""
    if not _SLUG_RE.fullmatch(s):
        return False
    try:
        (workspace_dir() / f"{s}.json").unlink()
    except FileNotFoundError:
        return False
    return True


def _check_mode(path: Path) -> None:
    if os.stat(path).st_mode & 0o077:
        raise WorkspaceError("mode")


def _write(d: Path, s: str, body: str) -> None:
    """Atomic owner-only write of <slug>.json, exactly like the memory store."""
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = d / f"{s}.json"
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(d):
        raise WorkspaceError("name")
    if path.exists():
        _check_mode(path)
    tmp = d / f".{s}.json.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise WorkspaceError("io") from exc
    os.chmod(path, 0o600)
