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
                  "open", "please", "latest", "last", "newest", "recent", "yesterday", "today"})
# jauto3b: recency words are never note names; about and called stay query words (B2)
# jauto3b r1: "most" and "wrote" left STOP too (_RECENCY_RE still guards the recency asks)
_EXCLUDED = frozenset(d.casefold() for d in VAULT_EXCLUDED_DIRS)
_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def clear_cache() -> None:
    """Test hook: drop the cached scans and the prepared index."""
    global _index
    _cache.clear()
    _index = {"names": None, "notes": (), "vocab": frozenset(), "found": {}}


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
    _prepared(out)  # round 1: tokenise here (the caller's thread), not in find() on the event loop
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
    if q == t:
        return True
    if len(q) < 4 or len(t) < 4 or 2 * min(len(q), len(t)) < 0.8 * (len(q) + len(t)):
        return False  # round 1: the ratio can never reach 0.8 at this length gap
    m = difflib.SequenceMatcher(None, q, t)
    return m.quick_ratio() >= 0.8 and m.ratio() >= 0.8


# round 1 (perf): one prepared index for the newest names tuple (scan builds it in the policy pool), and
# find() results for it, so a turn's parse and the overlay share one computation.
_index: dict = {"names": None, "notes": (), "vocab": frozenset(), "found": {}}


def _prepared(names: tuple[str, ...]) -> dict:
    global _index
    index = _index  # one read: scan (pool thread) swaps in a whole new dict, never edits this one
    if index["names"] is names or index["names"] == names:
        return index
    notes, vocab = [], set()
    for rel in names:
        parts = rel.split("/")
        stem = frozenset(tokens(os.path.splitext(parts[-1])[0]))
        folder = frozenset(tokens(" ".join(parts[:-1])))
        notes.append((rel, stem, stem | folder))
        vocab |= stem | folder
    _index = {"names": names, "notes": tuple(notes), "vocab": frozenset(vocab), "found": {}}
    return _index


def find(names: tuple[str, ...], words: str, *, literal: bool = False) -> list[str]:
    """Note paths a spoken name means: query tokens must hit the stem (or the folder words);
    the best hit count wins, all ties are kept. [] = no note matches."""
    q = tokens(words)
    if not q:
        return []
    # jauto3 round 2 (R2-3a): the cost is |q| x |vocab|; cap the ask so it cannot tie a pool worker
    seen: set[str] = set()
    q = [w for w in q if not (w in seen or seen.add(w))][:12]
    index = _prepared(names)
    digits = "".join(c for c in words if c.isdigit())  # jauto3 round 3 (R3-1): date words tie-break
    key = (tuple(q), literal, digits)
    hit = index["found"].get(key)  # jauto3 round 2 (R2-3b): one read, no check-then-index race with clear()
    if hit is not None:
        return list(hit)
    near = [frozenset(t for t in index["vocab"] if _match(w, t)) for w in q]  # |Q| x |vocab| ratios, once
    scored: list[tuple[int, str]] = []
    for rel, stem, every in index["notes"]:
        stem_hits = sum(1 for m in near if m & stem)
        if stem_hits < 1:
            continue
        hits = sum(1 for m in near if m & every)
        if not ((hits >= 2 and hits >= 0.6 * len(q)) or (literal and len(q) == 1 and stem_hits == 1)):
            continue
        scored.append((hits, rel))
    best = max((h for h, _ in scored), default=0)
    out = sorted(rel for h, rel in scored if h == best)
    if len(out) > 1 and digits:
        # jauto3 round 3 (R3-1): tokens() drops digits, so a tie the tie line spoke as dates
        # comes straight back; the query's digits narrow it, but never to nothing.
        narrowed = [r for r in out if digits in "".join(c for c in r.rsplit("/", 1)[-1] if c.isdigit())]
        out = narrowed or out
    if len(index["found"]) >= 64:
        index["found"].clear()
    index["found"][key] = tuple(out)
    return out


def speak_name(rel: str, keep_digits: bool = False) -> str:
    """What the overlay says for a found note: the stem's words, dashes and underscores as spaces, dates
    and other digit-only words dropped (round 1 U3) unless nothing else is left. keep_digits (round 2
    R2-1): spoken with the digit words, for ties that only a date or folder can tell apart."""
    stem = os.path.splitext(rel.rsplit("/", 1)[-1])[0]
    words = stem.replace("-", " ").replace("_", " ").split()
    return " ".join(words if keep_digits else ([w for w in words if not w.isdigit()] or words))
