"""jfollow10e: the one Jarvis Companion fact sheet (a vault note), read as DATA for three prompts.

The note sits at <vault>/Projects/Jarvis Companion/Capabilities.md. Any session can write it, so it is
checked here: no symlinks, inside the vault, a regular file, <= MAX_BYTES, strict UTF-8, cleaned, and
scanned for secrets and addresses. Anything wrong -> the repo fixture (public facts only). Never raises.
"""
from __future__ import annotations

import errno
import logging
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from common import hud

logger = logging.getLogger(__name__)

NOTE_REL = Path("Projects", "Jarvis Companion", "Capabilities.md")
FIXTURE = Path(__file__).resolve().parents[1] / "docs" / "jarvis-facts.md"
MAX_BYTES = 6000
STAT_EVERY_S = 30

# Zero-width and bidi controls: they hide text from a reader but not from a model.
_INVISIBLE = re.compile("[­؜᠎​-‏‪-‮⁠-⁤⁦-⁯﻿]")
_CTRL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
# Built from parts so the repo privacy grep never matches this file itself.
_SECRET = re.compile("|".join((
    r"sk-[A-Za-z0-9_\-]{4,}", r"\b(?i:bearer|token)[ =:]+[A-Za-z0-9._\-]{8,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"AKIA[0-9A-Z]{16}",
    r"xox[abprs]-[A-Za-z0-9-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"ts\." + "net", r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+",
    r"https?://", "/" + "Users/", "/" + "home/", r"~/",
)))

clock = time.monotonic  # tests swap it


@dataclass(frozen=True)
class Facts:
    text: str
    src: str     # vault | fixture
    reason: str  # ok | unset | missing | symlink | outside | not_file | too_big | bad_utf8 | secret | no_fixture


_cache: dict[str, tuple[float, tuple[int, int], Facts]] = {}  # path -> (checked at, (mtime_ns, size), facts)


def clean(text: str) -> str:
    """No tag brackets (the sheet cannot close its <facts> block), no control or invisible characters."""
    return _INVISIBLE.sub("", _CTRL.sub("", text.replace("<", "").replace(">", "")))


def _read_note(root: str) -> tuple[str | None, str, tuple[int, int] | None]:
    """(text, reason, (mtime_ns, size)); text None when the note cannot be used."""
    base = Path(root)
    path = base / NOTE_REL
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ELOOP:
            return None, "symlink", None
        if e.errno == errno.ENOENT:
            return None, "missing", None
        return None, "not_file", None
    try:
        if not os.path.realpath(path).startswith(os.path.realpath(base) + os.sep):
            return None, "outside", None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not_file", None
        if st.st_size > MAX_BYTES:
            return None, "too_big", None
        data = os.read(fd, MAX_BYTES + 1)
    except OSError:  # a directory reads as EISDIR
        return None, "not_file", None
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        return None, "too_big", None
    try:
        text = clean(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None, "bad_utf8", None
    if _SECRET.search(text):
        return None, "secret", None
    return text, "ok", (st.st_mtime_ns, st.st_size)


def _fixture(reason: str) -> Facts:
    try:
        return Facts(clean(FIXTURE.read_text(encoding="utf-8")[:MAX_BYTES]), "fixture", reason)
    except (OSError, UnicodeDecodeError):
        return Facts("", "fixture", "no_fixture")


def load(root: str | os.PathLike | None) -> Facts:
    """The vault note when it passes every check, else the fixture; one warning with the reason word."""
    if not root:
        return _fixture("unset")
    key = str(Path(root) / NOTE_REL)
    hit = _cache.get(key)
    now = clock()
    if hit is not None and now - hit[0] < STAT_EVERY_S:
        return hit[2]
    if hit is not None and hit[2].src == "vault":
        try:
            st = os.stat(key, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and (st.st_mtime_ns, st.st_size) == hit[1]:
                _cache[key] = (now, hit[1], hit[2])
                return hit[2]
        except OSError:
            pass
    text, reason, sig = _read_note(str(root))
    if text is None:
        logger.warning("facts note not used: %s", reason)
        facts = _fixture(reason)
    else:
        facts = Facts(text, "vault", "ok")
    _cache[key] = (now, sig or (0, 0), facts)
    return facts


def block(f: Facts) -> str:
    return "<facts>\n" + f.text + "\n</facts>"


def _card(text: str) -> list[str]:
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Card"
        elif inside and line.startswith("- "):
            lines.append(hud._text(line[2:], 80))
    return [ln for ln in lines if ln][:6]


def card_lines(f: Facts) -> list[str]:
    """The `## Card` section's bullet lines (<= 6, <= 80 chars each), else the fixture's."""
    return _card(f.text) or _card(_fixture("ok").text)
