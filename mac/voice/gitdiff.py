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
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from common.hud import MAX_BODY_LINES, MAX_ITEMS, MAX_LABEL, validate_card

_TIMEOUT = 10
_EDITOR_TIMEOUT = 2  # the editor CLI returns at once when healthy
# Every git call runs with repo config defused — the repo may come from an
# archive, so no fsmonitor, no hooks, no external diff driver, no pager, no ext::/fd:: remote helper,
# no signature check (that runs gpg.program). Program keys the prefix can't defuse: has_filters.
_GIT_PREFIX = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "diff.external=", "-c", "core.pager=cat", "-c", "log.showSignature=false",
               "-c", "protocol.ext.allow=never", "-c", "protocol.fd.allow=never"]
# Repo-scoped config keys whose value is a program git (or git-lfs) runs: gpg on a signature check,
# ssh/askpass/credential/uploadpack on fetch and push, editors, aliases, drivers, tools, hooks. Git's
# own names end in program/command/cmd/helper/editor/driver/askpass/proxy/hook; the rest are listed.
# A repo that sets any of them is refused whole, so no git form (Allowed or Confirmed) can run one.
_PROGRAM_KEY_RE = re.compile(
    r"^(filter\..+|alias\..+|pager\..+|sendemail\..+|man\..+|browser\..+|web\.browser|imap\.tunnel"
    r"|instaweb\..+|help\.browser|cvsimport\.cvsps"
    r"|core\.pager|interactive\.difffilter|diff\..+\.textconv|submodule\..+\.update"
    r"|remote\..+\.(uploadpack|receivepack|vcs)|(difftool|mergetool)\..+\.path"
    r"|lfs\.customtransfer\..+\.path|lfs\.extension\..+\.(clean|smudge)"
    r"|.*(program|command|cmd|helper|editor|driver|askpass|proxy|hook))$", re.I)
_OWNER_SCOPES = ("global", "system", "command", "unknown")
# Hunk header: -<start>[,<count>] +<start>[,<count>]; a missing ,count means 1 on either side.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Overlay labels: only integers and fixed words ever reach the app from diff data.
LABEL_RE = re.compile(r"^[0-9]{1,2}/[0-9]{1,2} (\+[0-9]{1,6}|changed|removed here)$")


def _git(repo: Path, *args: str) -> str | None:
    """One read-only git call; stdout on success, None on failure (no repo, no git, timeout)."""
    try:
        proc = subprocess.run(
            [*_GIT_PREFIX, *args], cwd=repo, capture_output=True, text=True, timeout=_TIMEOUT
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


def has_filters(repo: Path) -> bool:
    """True when repo-controlled config (local, worktree, includes) names a program git would run.

    Filters, diff drivers, gpg.program, core.sshCommand, aliases and the rest (_PROGRAM_KEY_RE):
    the overlay and the agent's git calls refuse such repos. The owner's own scopes are skipped:
    global, system, command (our prefix, the owner's env) and unknown (Apple git lists Xcode's
    built-in gitconfig, credential.helper, as "unknown"). Any other scope refuses, so local,
    worktree (.git/config.worktree) and any scope a later git adds fail closed. A file the repo
    pulls in with include.path / includeIf keeps the includer's scope (local / worktree; pinned in
    tests). --name-only: a key is listed whole (a subsection holding "=" can't cut it short) and
    no value can forge a line. No listing at all (git failed) refuses too.
    """
    out = _git(Path(repo), "config", "--includes", "--show-scope", "--name-only", "--list")
    if out is None:
        return True
    for line in out.splitlines():
        scope, _, key = line.partition("\t")
        if scope.strip() not in _OWNER_SCOPES and _PROGRAM_KEY_RE.match(key):
            return True
    return False


def last_merge(repo: Path) -> str | None:
    """Sha of the most recent merge commit; None when the repo has none (or git fails)."""
    out = _git(Path(repo), "log", "--merges", "-1", "--format=%H")
    return out.splitlines()[0].strip() if out else None


def hunks(repo: Path, base: str) -> list[dict]:
    """New-side line ranges of the working-tree diff against `base`.

    [{file, start, count, old}] in diff order (count = new-side lines, old = old-side
    lines). Deleted files (+++ /dev/null) are skipped; pure deletions are kept with
    count 0 so the app can still highlight the old line.
    """
    out = _git(Path(repo), "diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv", base)
    return _parse_hunks(out) if out else []


def hunks_between(repo: Path, a: str, b: str) -> list[dict]:
    """Same shape, for the commit-to-commit diff `a` -> `b` (e.g. a merge vs its first parent)."""
    out = _git(Path(repo), "diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-textconv", a, b)
    return _parse_hunks(out) if out else []


def _parse_hunks(out: str) -> list[dict]:
    result: list[dict] = []
    file: str | None = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            body = line[4:]  # "b/<path>" (or "/dev/null" for a deleted file)
            file = None if body == "/dev/null" else body[2:]
            continue
        m = _HUNK_RE.match(line)
        if m and file:
            result.append({"file": file, "start": int(m[2]), "count": int(m[3] or 1),
                           "old": int(m[1] or 1)})
    return result


@dataclass
class Range:
    """What a "what changed" ask resolved to: kind uncommitted|since_merge|merge_itself|none,
    the diff base (and head, for commit-to-commit ranges), and its hunks."""
    kind: str
    base: str | None
    head: str | None
    hunks: list[dict]


def pick_range(repo: Path, sub: str) -> Range:
    """Pick what to show for `sub`: "auto" prefers uncommitted edits, then the last merge's
    own new-side work (merge_itself) or what landed since it (since_merge); "merge" skips
    the uncommitted check. Nothing to show -> kind "none" (base still says what was checked)."""
    if sub == "auto":
        h = hunks(repo, "HEAD")
        if h:
            return Range("uncommitted", "HEAD", None, h)
    m = last_merge(repo)
    if m is None:
        return Range("none", None, None, [])
    h = hunks(repo, m)
    if h:
        return Range("since_merge", m, None, h)
    h = hunks_between(repo, f"{m}^", m)
    if h:
        return Range("merge_itself", f"{m}^", m, h)
    return Range("none", m, None, [])


def hunk_label(h: dict, i: int, n: int) -> str:
    """Short overlay label for hunk `i` of `n`: "+k" added, "changed", or "removed here"."""
    if h["old"] == 0:
        return f"{i}/{n} +{h['count']}"
    if h["count"] == 0:
        return f"{i}/{n} removed here"
    return f"{i}/{n} changed"


def hunk_diff(repo: Path, base: str, rel: str, start: int, limit: int = 3000,
              head: str | None = None) -> str:
    """Unified=3 hunk (header + body) covering new-side line `start` of `rel`; "" when nothing matches.

    Exact hunk first; else the last hunk starting at or before `start` (the user's line
    sits in its context or the removal just after it). With `head`, diff base -> head
    (commit-to-commit) instead of the working tree.
    """
    out = _git(Path(repo), "diff", "--unified=3", "--no-color", "--no-ext-diff", "--no-textconv",
               *([base, head] if head else [base]), "--", rel)
    if not out:
        return ""
    parsed: list[tuple[int, int, str]] = []  # (new start, new count, hunk text)
    cur: list[str] = []
    head: tuple[int, int] | None = None
    for line in out.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            if head:
                parsed.append((*head, "\n".join(cur)))
            head = (int(m[2]), int(m[3] or 1))
            cur = [line]
        elif head:
            cur.append(line)
    if head:
        parsed.append((*head, "\n".join(cur)))
    for s, c, text in parsed:
        if s <= start <= s + max(c, 1) - 1:
            return text[:limit]
    before = [(s, text) for s, _, text in parsed if s <= start]
    return max(before, key=lambda st: st[0])[1][:limit] if before else ""


def blame_commits(repo: Path, rel: str, a: int, b: int, head: str | None = None,
                  max_n: int = 3) -> tuple[list[dict], bool]:
    """Up to `max_n` commits behind lines a..b of `rel`, newest committer time first, as
    display-clean {subject, date, sentence} dicts; True when a blamed line is uncommitted.
    With `head`, blame that commit's version of the file instead of the working tree."""
    args = ["blame", "--no-textconv", "-L", f"{a},{b}", "--porcelain"]
    if head:
        args.append(head)
    out = _git(Path(repo), *args, "--", rel)
    if not out:
        return [], False
    shas: list[str] = []
    uncommitted = False
    for line in out.splitlines():
        m = re.match(r"^([0-9a-f]{40}) \d+ \d+", line)
        if not m:
            continue
        if m[1] == "0" * 40:  # the all-zero sha marks not-committed-yet lines
            uncommitted = True
        elif m[1] not in shas:
            shas.append(m[1])
    rows: list[tuple[int, str, str, str]] = []  # (committer time, subject, date, body)
    for sha in shas:
        log = _git(Path(repo), "log", "-1", "--no-show-signature",
                   "--format=%ct%x00%s%x00%as%x00%b", sha)
        if not log:
            continue
        ct, subject, date, body = log.split("\x00", 3)
        rows.append((int(ct), subject, date, body))
    rows.sort(key=lambda r: -r[0])  # newest first; ties keep blame (line) order
    return [{"subject": _clean_text(subject)[:120], "date": date,
             "sentence": first_sentence(body)} for _, subject, date, body in rows[:max_n]], uncommitted


def _clean_text(s: str) -> str:
    """Commit text made display-safe: no tags, angle brackets, control chars or run-on spaces."""
    s = re.sub(r"<[^>]*>", "", s)
    s = s.replace("<", "").replace(">", "")
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("C"))
    return " ".join(s.split())


def first_sentence(body: str) -> str:
    """Cleaned body up to and including its first . ! or ?; first 160 chars when it has none."""
    b = _clean_text(body)
    if not b:
        return ""
    head = b[:160]
    for i, ch in enumerate(head):
        if ch in ".!?":
            return head[:i + 1]
    return head


def diff_text(repo: Path, base: str, limit: int = 12000, head: str | None = None) -> str:
    """First `limit` chars of the plain diff against `base` (commit-to-commit with `head`);
    "" on failure."""
    out = _git(Path(repo), "diff", "--no-color", "--no-ext-diff", "--no-textconv",
               *([base, head] if head else [base]))
    return (out or "")[:limit]


# Editor CLIs live off PATH inside the macOS app bundles: tried in order when `which` misses.
EDITOR_CANDIDATES = {
    "code": ("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
             "~/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
             "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders",
             "/Applications/VSCodium.app/Contents/Resources/app/bin/codium"),
    "cursor": ("/Applications/Cursor.app/Contents/Resources/app/bin/cursor",),
}


def editor_path(editor: str) -> str | None:
    """CLI of `editor`: on PATH, else the first bundled app binary that exists and runs;
    None for an unknown editor or when nothing is found."""
    if editor not in EDITOR_CANDIDATES:
        return None
    found = shutil.which(editor)
    if found:
        return found
    for cand in EDITOR_CANDIDATES[editor]:
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def open_in_editor(repo: Path, file: str, line: int, *, run=subprocess.run,
                   editor: str = "code", fallback: bool = True) -> str:
    """Open <repo/file> at `line` in VS Code (`editor`: "code" or "cursor"); fall back to the default
    text editor, or with fallback=False do nothing more.

    Returns "code"|"text-editor"|"none". `file` is repo-relative and untrusted: a path
    resolving outside the repo is refused. The fallback is `open -t`, never plain
    `open` — a file checked out of a repo must not be executed (.command/.workflow).
    """
    if editor not in ("code", "cursor"):
        raise ValueError(f"unknown editor: {editor!r}")
    root = Path(repo).resolve()
    target = (root / file).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"path escapes the repo: {file!r}")
    exe = editor_path(editor)
    if exe is not None:
        try:
            if ":" not in target.name:  # --goto reads a trailing :N[:M] as line/column: `a:9` would open `a`
                proc = run([exe, "--goto", f"{target}:{int(line)}"], check=False, capture_output=True,
                           timeout=_EDITOR_TIMEOUT)
                if proc is not None and proc.returncode == 0:
                    return "code"
        except FileNotFoundError:
            pass
        except subprocess.SubprocessError:  # a hung CLI (stuck IPC): don't open a second editor too
            return "none"
    if not fallback:
        return "none"
    try:
        run(["open", "-t", str(target)], check=False, capture_output=True, timeout=_EDITOR_TIMEOUT)
    except subprocess.SubprocessError:
        return "none"
    return "text-editor"


def files_phrase(n: int) -> str:
    """Count of files for the stat card and the spoken summary: 1 file, 3 files."""
    return f"{n} file" if n == 1 else f"{n} files"


def diff_cards(hunks: list[dict], base: str, title: str = "Changed since last merge",
               empty: str = "No changes since the last merge") -> list[dict]:
    """Stat + bar + "where it changed" info cards summarizing `hunks()` output, validated by common.hud.

    The stat (and the empty-case info card) carry `title`, the empty card says `empty`.
    Empty hunks (or a failed git call upstream) yield one info card instead.
    """
    short = base[:8]
    if not hunks:
        card = validate_card(
            {"type": "info", "id": "gd-stat", "title": title, "body": [empty]}
        )
        return [card] if card else []
    per_file: dict[str, int] = {}
    for h in hunks:
        per_file[h["file"]] = per_file.get(h["file"], 0) + h["count"]
    cards = [
        validate_card(
            {"type": "stat", "id": "gd-stat", "title": title,
             "value": files_phrase(len(per_file)), "label": short}
        ),
        validate_card(
            {"type": "bar", "id": "gd-bar", "title": "Changed lines by file",
             "items": [
                 {"label": Path(f).name[:MAX_LABEL], "value": n}
                 for f, n in sorted(per_file.items(), key=lambda kv: -kv[1])[:MAX_ITEMS]
             ],
             "caption": f"git diff {short}"},
        ),
        # Where exactly: one line per file with its changed line ranges (top 6 files).
        validate_card(
            {"type": "info", "id": "gd-where", "title": "Where it changed",
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
