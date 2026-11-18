"""jfollow11c: read-only answers from the owner's git repos (GET only, behind the phone bearer).

Repos are the direct child dirs (or the root itself) holding `.git` under the dirs in
`JARVIS_REPO_ROOTS` (joined by os.pathsep; unset = off). Every path is checked by name before any
open: relative, no `..`, no `.git`, no secret-looking names; then it must stay inside the repo after
symlinks are resolved, open with O_NOFOLLOW as a regular, small, UTF-8 text file. No write and no
command of any kind. Text goes out masked; the caller treats it as untrusted DATA.
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
import threading
import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from gateway.auth import require_bearer_any

router = APIRouter(dependencies=[Depends(require_bearer_any("ios_token"))])

ENV_KEY = "JARVIS_REPO_ROOTS"
MAX_REPOS = 64
MAX_PATH = 300
MAX_READ_BYTES = 1_000_000
MAX_TEXT = 12_000
SNIFF = 8192
SEARCH_MAX_BYTES = 512 * 1024
SEARCH_MAX_FILES = 5000
SEARCH_MAX_S = 3.0
MAX_HITS = 8
HIT_CHARS = 300

_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_WORD = re.compile(r"[A-Za-z0-9_.-]{2,40}")
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "Pods",
              "DerivedData"}
_DENY = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.p8", "*.ppk", "*.keystore", "*.jks",
         "*.kdbx", "id_rsa*", "id_ed25519*", "id_ecdsa*", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
         "credentials*", "*secret*", "*token*", "service-account*.json", "secrets")
# A copy of gateway/overlay.py `_SECRET_SAY`: the mini PC deploy has no overlay.py to import from.
_MASK = tuple((re.compile(p), r) for p, r in (
    (r"sk-[A-Za-z0-9_\-]{4,}", "[key]"), (r"(?i)\b(bearer|token)[ =:]+[A-Za-z0-9._\-]{8,}", r"\1 [key]"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "[key]"), (r"github_pat_[A-Za-z0-9_]{20,}", "[key]"),
    (r"AKIA[0-9A-Z]{16}", "[key]"), (r"xox[abprs]-[A-Za-z0-9-]{10,}", "[key]"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "[key]")))
_STATUS = {"off": 404, "no_repo": 404, "bad_path": 400, "bad_query": 400, "git": 403, "secret_name": 403,
           "outside": 403, "not_file": 404, "too_big": 413, "binary": 415, "bad_utf8": 415, "busy": 429}
_search_lock = threading.Lock()
clock = time.monotonic  # tests swap it


class Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def mask(text: str) -> str:
    for pattern, repl in _MASK:
        text = pattern.sub(repl, text)
    return text


def _denied(segment: str) -> bool:
    low = segment.lower()
    return any(fnmatch.fnmatchcase(low, p) for p in _DENY)


def _roots() -> list[str]:
    raw = os.environ.get(ENV_KEY, "")
    return [r for r in raw.split(os.pathsep) if r.strip() and os.path.isabs(r.strip())]


def _is_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git")) and not os.path.exists(os.path.join(path, ".obsidian"))


def repos() -> dict[str, str]:
    """name -> realpath of each listed repo (first root wins on a name clash), at most MAX_REPOS."""
    found: dict[str, str] = {}
    for root in _roots():
        real_root = os.path.realpath(root)
        if any(s.lower() == "secrets" for s in real_root.split(os.sep)):
            continue
        cands = [real_root]
        try:
            cands += [os.path.join(root, n) for n in sorted(os.listdir(real_root))]
        except OSError:
            continue
        for cand in cands:
            real = os.path.realpath(cand)
            name = os.path.basename(real if cand == real_root else cand)
            if real != real_root and not real.startswith(real_root + os.sep):
                continue
            if any(s.lower() == "secrets" for s in real[len(real_root):].split(os.sep)):
                continue
            if not _NAME.fullmatch(name) or name in found or not _is_repo(real):
                continue
            found[name] = real
            if len(found) >= MAX_REPOS:
                return found
    return found


def _repo(name: str) -> str:
    if not _roots():
        raise Refused("off")
    real = repos().get(name) if _NAME.fullmatch(name) else None
    if real is None:
        raise Refused("no_repo")
    return real


def check_path(path: str) -> list[str]:
    """Rules 1-3 (Design B3), by name only: no filesystem call. Returns the segments."""
    if not path or len(path) > MAX_PATH or "\x00" in path or "\\" in path or path.startswith("/"):
        raise Refused("bad_path")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise Refused("bad_path")
    if any(p.lower() == ".git" for p in parts):
        raise Refused("git")
    if any(_denied(p) for p in parts):
        raise Refused("secret_name")
    return parts


def _readme(repo: str) -> str:
    try:
        names = sorted(n for n in os.listdir(repo) if n.lower().startswith("readme"))
    except OSError:
        names = []
    for n in names:
        if os.path.isfile(os.path.join(repo, n)) and not os.path.islink(os.path.join(repo, n)):
            return n
    raise Refused("not_file")


def _open_inside(repo: str, rel: str) -> int:
    """Rule 4 up to the open: realpath inside the repo, O_NOFOLLOW, the same inode, a regular file."""
    full = os.path.join(repo, rel)
    real = os.path.realpath(full)
    if not real.startswith(repo + os.sep):
        raise Refused("outside")
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise Refused("not_file") from None
    try:
        st = os.fstat(fd)
        same = os.stat(real)
        if (st.st_dev, st.st_ino) != (same.st_dev, same.st_ino):
            raise Refused("outside")
        if not stat.S_ISREG(st.st_mode):
            raise Refused("not_file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _text(fd: int, limit: int) -> tuple[str, int]:
    try:
        size = os.fstat(fd).st_size
        if size > limit:
            raise Refused("too_big")
        data = os.read(fd, limit + 1)
    except OSError:
        raise Refused("not_file") from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise Refused("too_big")
    if b"\x00" in data[:SNIFF]:
        raise Refused("binary")
    try:
        return data.decode("utf-8"), len(data)
    except UnicodeDecodeError:
        raise Refused("bad_utf8") from None


def read_file(name: str, path: str) -> dict:
    repo = _repo(name)
    rel = _readme(repo) if path == "README" else "/".join(check_path(path))
    if path == "README":
        check_path(rel)
    text, size = _text(_open_inside(repo, rel), MAX_READ_BYTES)
    return {"repo": name, "path": rel, "text": mask(text[:MAX_TEXT]), "truncated": len(text) > MAX_TEXT,
            "bytes": size}


def query_words(q: str) -> list[str]:
    words = q.split()
    if not 1 <= len(words) <= 6 or not all(_WORD.fullmatch(w) for w in words):
        raise Refused("bad_query")
    return [w.lower() for w in words]


def search(name: str, q: str) -> dict:
    words = query_words(q)
    repo = _repo(name)
    if not _search_lock.acquire(timeout=SEARCH_MAX_S):
        raise Refused("busy")
    try:
        return {"repo": name, **_search(repo, words)}
    finally:
        _search_lock.release()


def _search(repo: str, words: list[str]) -> dict:
    start, seen, capped = clock(), 0, False
    found: list[tuple[int, str, int, str]] = []  # (score, rel, line no, snippet)
    for top, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and d.lower() != ".git" and not _denied(d)
                         and not os.path.islink(os.path.join(top, d)))
        for fname in sorted(files):
            if seen >= SEARCH_MAX_FILES or clock() - start > SEARCH_MAX_S:
                capped = True
                break
            rel = os.path.relpath(os.path.join(top, fname), repo).replace(os.sep, "/")
            try:
                check_path(rel)
                fd = _open_inside(repo, rel)
                text, _ = _text(fd, SEARCH_MAX_BYTES)
            except Refused:
                continue
            seen += 1
            hit = _score(rel, text, words)
            if hit:
                found.append(hit)
        if capped:
            break
    found.sort(key=lambda h: (-h[0], h[1]))
    hits = [{"path": rel, "line": ln, "text": snip} for _, rel, ln, snip in found[:MAX_HITS]]
    return {"hits": hits, "capped": capped}


def _score(rel: str, text: str, words: list[str]) -> tuple[int, str, int, str] | None:
    lines = text.splitlines()
    best, best_n, score = -1, 0, 2 * sum(w in rel.lower() for w in words)
    for i, line in enumerate(lines):
        n = sum(w in line.lower() for w in words)
        score += n
        if n > best_n:
            best, best_n = i, n
    if best < 0:
        return None
    snip = " ".join(s.strip() for s in lines[max(0, best - 1):best + 2])
    return score, rel, best + 1, mask(snip)[:HIT_CHARS]


def _refused(e: Refused) -> JSONResponse:
    return JSONResponse({"error": e.reason}, status_code=_STATUS.get(e.reason, 400))


@router.get("/central/repos")
def list_repos() -> dict:
    if not _roots():
        return {"state": "off", "repos": []}
    return {"state": "on", "repos": sorted(repos())}


@router.get("/central/repos/{name}/file")
def repo_file(name: str, path: str = Query(default="")):
    try:
        return read_file(name, path)
    except Refused as e:
        return _refused(e)


@router.get("/central/repos/{name}/search")
def repo_search(name: str, q: str = Query(default="")):
    try:
        return search(name, q)
    except Refused as e:
        return _refused(e)
