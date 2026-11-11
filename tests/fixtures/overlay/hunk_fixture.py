"""Builds a temp git repo whose diff against the last merge has one added, one changed
and one removed hunk in sample.py, each from its own named commit.

Used by tests/test_overlay_git.py (hunk counts, labels, blame, hunk_diff) and by the
live Mac check: open sample.py in VS Code and ask to highlight the changes since the
last merge.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# The box's own git config must never leak into the fixture's history.
_GIT = ["git", "-c", "user.name=Hunk Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run([*_GIT, *args], cwd=root, capture_output=True, text=True,
                          env=_ENV, check=True, timeout=30)
    return proc.stdout.strip()


def build(root: Path, extra: bool = False) -> dict:
    """Create the repo in `root` (empty or missing folder); returns the ground-truth dict."""
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"refusing: {root} is not empty")
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    sample = [f"L{k:03d} = {k}" for k in range(1, 201)]
    _commit(root, "sample.py", sample, "base")
    _git(root, "checkout", "-qb", "side")
    (root / "side.txt").write_text("side\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "side work")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "--no-ff", "-m", "Merge side", "side")
    merge = _git(root, "rev-parse", "HEAD")
    sample[139:139] = ['L901 = "retry"', "L902 = 0", "L903 = 3"]  # new lines 140-142
    _commit(root, "sample.py", sample, "Add the retry counter")
    sample[148] = "L904 = 146  # timeout renamed"  # line 149 was L146 = 146
    _commit(root, "sample.py", sample, "Rename the timeout flag")
    del sample[155:157]  # lines 156-157 were L153 = 153 / L154 = 154
    _commit(root, "sample.py", sample, "Drop the unused cache lines")
    if extra:  # a file with more changed lines than sample.py
        _commit(root, "other.py", [f"O{i:02d} = {i}" for i in range(1, 13)], "Add other module")
    return {
        "repo": str(root), "file": "sample.py", "merge": merge,
        "hunks": [
            {"start": 140, "end": 142, "kind": "added", "subject": "Add the retry counter",
             "tokens": ["L901", "L902", "L903"]},
            {"start": 149, "end": 149, "kind": "changed", "subject": "Rename the timeout flag",
             "tokens": ["L904"]},
            {"start": 155, "end": 155, "kind": "removed", "subject": "Drop the unused cache lines",
             "tokens": ["L152"]},
        ],
    }


def _commit(root: Path, name: str, lines: list[str], subject: str) -> None:
    (root / name).write_text("\n".join(lines) + "\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", subject)


def main(argv: list[str]) -> None:
    extra = "--extra" in argv[1:]
    dirs = [a for a in argv[1:] if a != "--extra"]
    if len(dirs) != 1:
        raise SystemExit("usage: hunk_fixture.py <dir> [--extra]")
    print(json.dumps(build(Path(dirs[0]), extra), indent=2))


if __name__ == "__main__":
    main(sys.argv)
