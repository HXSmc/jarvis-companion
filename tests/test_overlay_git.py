"""Overlay git-diff helpers against a real temp repo: merge detection, hunk ranges, cards.

Git runs for real (the parser must eat actual `git diff --unified=0` output), identity
comes from -c flags with GIT_CONFIG_GLOBAL=/dev/null so the box's own git config never
leaks in. After a --no-ff merge the working tree gets uncommitted edits and the tests
assert the exact new-side ranges. Only `code`/`open` are stubbed — they don't exist on
Linux; d.py exists in the merge and is deleted after it to prove deleted files are
skipped (+++ /dev/null).
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from common.hud import validate_card
from mac.voice import gitdiff

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}

# Working tree after the fixture's post-merge edits (a.py line 1 deleted, original
# lines 3-4 edited, b.py +2, c.py new with 5 lines, d.py deleted -> skipped).
EXPECTED_HUNKS = [
    {"file": "a.py", "start": 0, "count": 0},
    {"file": "a.py", "start": 2, "count": 2},
    {"file": "b.py", "start": 4, "count": 2},
    {"file": "c.py", "start": 1, "count": 5},
]


def _git(repo, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=Overlay Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, env=GIT_ENV, check=True, timeout=10,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Real repo: base commit -> feature branch -> --no-ff merge -> uncommitted edits."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text("alpha1\nalpha2\nalpha3\nalpha4\nalpha5\n")
    (r / "b.py").write_text("bee1\nbee2\nbee3\n")
    (r / "d.py").write_text("dee1\ndee2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "feature")
    (r / "feat.py").write_text("feat1\nfeat2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "feature work")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "--no-ff", "-m", "merge feature", "feature")
    merge = _git(r, "rev-parse", "HEAD")
    (r / "a.py").write_text("alpha2\nalpha3 edited\nalpha4 edited\nalpha5\n")
    (r / "b.py").write_text("bee1\nbee2\nbee3\nbee4\nbee5\n")
    (r / "c.py").write_text("cee1\ncee2\ncee3\ncee4\ncee5\n")
    (r / "d.py").unlink()
    _git(r, "add", "-A")  # stage the edits: git diff <base> only sees tracked files
    return r, merge


def test_last_merge_is_the_merge_sha(repo):
    r, merge = repo
    assert gitdiff.last_merge(r) == merge


def test_last_merge_none_without_merges(tmp_path):
    r = tmp_path / "plain"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "x.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "only commit")
    assert gitdiff.last_merge(r) is None


def test_hunks_exact_files_and_new_side_ranges(repo):
    r, merge = repo
    assert gitdiff.hunks(r, merge) == EXPECTED_HUNKS


def test_repo_for_document_in_repo(repo):
    r, _ = repo
    assert gitdiff.repo_for(str(r / "a.py")).resolve() == r.resolve()


def test_repo_for_env_fallback(tmp_path, monkeypatch):
    fallback = tmp_path / "elsewhere"
    monkeypatch.setenv("OVERLAY_GIT_REPO", str(fallback))
    assert gitdiff.repo_for(None) == fallback
    monkeypatch.delenv("OVERLAY_GIT_REPO", raising=False)
    assert gitdiff.repo_for(None) is None


def test_open_in_editor_code(repo):
    r, _ = repo
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    assert gitdiff.open_in_editor(r, "a.py", 3, run=fake_run) == "code"
    assert calls == [["code", "--goto", f"{(r / 'a.py').resolve()}:3"]]


def test_open_in_editor_open_when_code_missing(repo):
    r, _ = repo
    calls = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "code":
            raise FileNotFoundError
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    assert gitdiff.open_in_editor(r, "a.py", 3, run=fake_run) == "open"
    assert calls == [["open", str((r / "a.py").resolve())]]


def test_open_in_editor_refuses_traversal(repo):
    r, _ = repo
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    with pytest.raises(ValueError):
        gitdiff.open_in_editor(r, "../outside.py", 1, run=fake_run)
    assert calls == []  # refused before anything runs


def test_diff_cards_stat_and_bar(repo):
    r, merge = repo
    cards = gitdiff.diff_cards(EXPECTED_HUNKS, merge)
    assert [c["type"] for c in cards] == ["stat", "bar", "info"]
    stat, bar, where = cards
    assert stat["value"] == "3 files"
    assert stat["label"] == merge[:8]
    assert [(i["label"], i["value"]) for i in bar["items"]] == [
        ("c.py", 5), ("a.py", 2), ("b.py", 2),
    ]
    assert bar["caption"] == f"git diff {merge[:8]}"
    assert where["body"][0] == "c.py: 1-5"
    assert all(validate_card(c) is not None for c in cards)


def test_diff_cards_empty_hunks_give_info_card(repo):
    _, merge = repo
    cards = gitdiff.diff_cards([], merge)
    assert len(cards) == 1
    assert cards[0]["type"] == "info"
    assert cards[0]["body"] == ["No changes since the last merge"]
    assert validate_card(cards[0]) is not None
