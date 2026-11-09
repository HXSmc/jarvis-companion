"""Git diff helpers for the overlay's "highlight the changes after the last merge" intent.

Finds the repo of the front document, its last merge commit, and the new-side line
ranges of the working-tree diff against it. `hunks` output feeds both the `lines`
event (the app OCR-maps the editor gutter to screen y) and `diff_cards` (stat + bar
cards). Every git/editor call is an arg list with cwd + timeout, never a shell
string; the document path is untrusted and resolved against the repo root so `..`
traversal can't open files outside the repo.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from common.hud import MAX_BODY_LINES, MAX_ITEMS, MAX_LABEL, validate_card

_TIMEOUT = 10
# New side of a unified=0 hunk header: +<start>[,<count>]; a missing ,count means 1.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(repo: Path, *args: str) -> str | None:
    """One read-only git call; stdout on success, None on failure (no repo, no git, timeout)."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None


def repo_for(document: str | None) -> Path | None:
    """Repo root of the front document's folder, else $OVERLAY_GIT_REPO, else None.

    A document whose parent doesn't exist (or isn't in a repo) falls through to the env.
    """
    if document:
        parent = Path(document).expanduser().parent
        if parent.is_dir():
            out = _git(parent, "rev-parse", "--show-toplevel")
            if out:
                return Path(out.splitlines()[0].strip())
    env = os.environ.get("OVERLAY_GIT_REPO")
    return Path(env).expanduser() if env else None


def last_merge(repo: Path) -> str | None:
    """Sha of the most recent merge commit; None when the repo has none (or git fails)."""
    out = _git(Path(repo), "log", "--merges", "-1", "--format=%H")
    return out.splitlines()[0].strip() if out else None


def hunks(repo: Path, base: str) -> list[dict]:
    """New-side line ranges of the working-tree diff against `base`.

    [{file, start, count}] in diff order. Deleted files (+++ /dev/null) are skipped;
    pure deletions are kept with count 0 so the app can still highlight the old line.
    """
    out = _git(Path(repo), "diff", "--unified=0", "--no-color", "--no-ext-diff", base)
    if not out:
        return []
    result: list[dict] = []
    file: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            body = line[4:]  # "b/<path>" (or "/dev/null" for a deleted file)
            file = None if body == "/dev/null" else body[2:]
            continue
        m = _HUNK_RE.match(line)
        if m and file:
            result.append({"file": file, "start": int(m[1]), "count": int(m[2] or 1)})
    return result


def open_in_editor(repo: Path, file: str, line: int, *, run=subprocess.run) -> str:
    """Open <repo/file> at `line` in VS Code; fall back to macOS `open`. Returns "code"|"open".

    `file` is repo-relative and untrusted: a path resolving outside the repo is refused.
    """
    root = Path(repo).resolve()
    target = (root / file).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"path escapes the repo: {file!r}")
    try:
        proc = run(["code", "--goto", f"{target}:{line}"], check=False, capture_output=True)
        if proc is not None and proc.returncode == 0:
            return "code"
    except FileNotFoundError:
        pass
    run(["open", str(target)], check=False, capture_output=True)
    return "open"


def diff_cards(hunks: list[dict], base: str) -> list[dict]:
    """Stat + bar + "where it changed" info cards summarizing `hunks()` output, validated by common.hud.

    Empty hunks (or a failed git call upstream) yield one info card instead.
    """
    short = base[:8]
    if not hunks:
        card = validate_card(
            {"type": "info", "title": "Changed since last merge",
             "body": ["No changes since the last merge"]}
        )
        return [card] if card else []
    per_file: dict[str, int] = {}
    for h in hunks:
        per_file[h["file"]] = per_file.get(h["file"], 0) + h["count"]
    cards = [
        validate_card(
            {"type": "stat", "title": "Changed since last merge",
             "value": f"{len(per_file)} files", "label": short}
        ),
        validate_card(
            {"type": "bar", "title": "Changed lines by file",
             "items": [
                 {"label": Path(f).name[:MAX_LABEL], "value": n}
                 for f, n in sorted(per_file.items(), key=lambda kv: -kv[1])[:MAX_ITEMS]
             ],
             "caption": f"git diff {short}"},
        ),
        # Where exactly: one line per file with its changed line ranges (top 6 files).
        validate_card(
            {"type": "info", "title": "Where it changed",
             "body": [
                 f"{f}: " + ", ".join(_span(h) for h in hunks if h["file"] == f)
                 for f, _ in sorted(per_file.items(), key=lambda kv: -kv[1])[:MAX_BODY_LINES]
             ]},
        ),
    ]
    return [c for c in cards if c]


def _span(h: dict) -> str:
    end = h["start"] + max(h["count"], 1) - 1
    return str(h["start"]) if end == h["start"] else f"{h['start']}-{end}"
