"""jauto3 (owner 3A): spoken note names -> vault notes. No model, no network; vault file
names never leave this module except to the overlay's own opener.

Every path comes from a bounded, symlink-free walk of the notes vault; each spoken name is
matched by word tokens only (fuzzy tokens for mishears), so a transcript can steer which
note opens but never write a path, a URL or a command.
"""
from __future__ import annotations

import difflib
import os
import re
import time

from mac.voice.agent_policy import _SECRET_DIR, VAULT_EXCLUDED_DIRS

DEPTH_MAX = 6  # jauto3: files directly in the vault are depth 1; a/b/c/d/e/f.md is depth 6
DIRS_MAX = 4000
FILES_MAX = 20000
CACHE_S = 30.0
STOP = frozenset({"the", "a", "an", "my", "of", "and", "in", "on", "to", "for", "from",
                  "note", "notes", "node", "nodes", "no", "not", "folder", "file", "md",
                  "open", "please"})
_EXCLUDED = frozenset(d.casefold() for d in VAULT_EXCLUDED_DIRS)
_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def clear_cache() -> None:
    """Test hook: drop the cached scans."""
    _cache.clear()


def scan(vault: str | None) -> tuple[str, ...]:
    """Vault-relative `/`-separated paths of real (non-symlink) `.md` files, sorted; () when
    the vault is missing/unreadable. Bounded by DEPTH_MAX/DIRS_MAX/FILES_MAX; secrets and
    dot folders are never walked."""
    if not vault:
        return ()
    cached = _cache.get(vault)
    if cached is not None and time.monotonic() - cached[0] < CACHE_S:
        return cached[1]
    found: list[str] = []
    dirs = 0
    pending = [(vault, "", 1)]  # (absolute dir, relative prefix, depth of the files inside)
    while pending and dirs < DIRS_MAX and len(found) < FILES_MAX:
        absd, rel, depth = pending.pop()
        dirs += 1
        try:
            entries = list(os.scandir(absd))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    name = entry.name.casefold()
                    if entry.name.startswith(".") or name == _SECRET_DIR or name in _EXCLUDED:
                        continue
                    if depth + 1 <= DEPTH_MAX:  # jauto3: never descend into a depth-6 dir
                        pending.append((entry.path, f"{rel}{entry.name}/", depth + 1))
                elif entry.is_file(follow_symlinks=False) and not entry.name.startswith(".") \
                        and entry.name.casefold().endswith(".md"):
                    found.append(rel + entry.name)
                    if len(found) >= FILES_MAX:
                        break
            except OSError:  # a stat that fails skips the entry, never the walk
                continue
    out = tuple(sorted(found))
    _cache[vault] = (time.monotonic(), out)
    return out


def tokens(s: str) -> list[str]:
    """Words that match: lowercase alnum runs, no pure digits, versus/v -> vs, STOP dropped."""
    out = []
    for w in re.split(r"[^a-z0-9]+", s.lower()):
        if not w or w.isdigit():
            continue
        if w in ("versus", "v"):
            w = "vs"
        if w in STOP:
            continue
        out.append(w)
    return out


def _match(q: str, t: str) -> bool:
    return q == t or (len(q) >= 4 and len(t) >= 4
                      and difflib.SequenceMatcher(None, q, t).ratio() >= 0.8)


def find(names: tuple[str, ...], words: str, *, literal: bool = False) -> list[str]:
    """Note paths a spoken name means: query tokens must hit the stem (or the folder words);
    the best hit count wins, all ties are kept. [] = no note matches."""
    q = tokens(words)
    if not q:
        return []
    scored: list[tuple[int, str]] = []
    for rel in names:
        parts = rel.split("/")
        stem = os.path.splitext(parts[-1])[0]
        s_tokens = tokens(stem)
        f_tokens = tokens(" ".join(parts[:-1]))
        hits = sum(1 for t in q if any(_match(t, x) for x in s_tokens + f_tokens))
        stem_hits = sum(1 for t in q if any(_match(t, x) for x in s_tokens))
        if stem_hits < 1:
            continue
        if not ((hits >= 2 and hits >= 0.6 * len(q)) or (literal and len(q) == 1 and stem_hits == 1)):
            continue
        scored.append((hits, rel))
    if not scored:
        return []
    best = max(h for h, _ in scored)
    return sorted(rel for h, rel in scored if h == best)


def speak_name(rel: str) -> str:
    """What the overlay says for a found note: the stem, dashes and underscores as spaces."""
    stem = os.path.splitext(rel.rsplit("/", 1)[-1])[0]
    return re.sub(r" {2,}", " ", stem.replace("-", " ").replace("_", " ")).strip()
