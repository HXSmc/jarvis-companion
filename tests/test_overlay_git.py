"""Overlay git-diff helpers against a real temp repo: merge detection, hunk ranges, cards.

Git runs for real (the parser must eat actual `git diff --unified=0` output), identity
comes from -c flags with GIT_CONFIG_GLOBAL=/dev/null so the box's own git config never
leaks in. After a --no-ff merge the working tree gets uncommitted edits and the tests
assert the exact new-side ranges. Only `code`/`open` are stubbed — they don't exist on
Linux; d.py exists in the merge and is deleted after it to prove deleted files are
skipped (+++ /dev/null).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from common.hud import validate_card
from conftest import TOKEN
from gateway import overlay
from mac.voice import gitdiff
from test_overlay_service import make_app, make_deps, says, turn

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}

# Working tree after the fixture's post-merge edits (a.py line 1 deleted, original
# lines 3-4 edited, b.py +2, c.py new with 5 lines, d.py deleted -> skipped).
# old = old-side count of each unified=0 hunk header.
EXPECTED_HUNKS = [
    {"file": "a.py", "start": 0, "count": 0, "old": 1},
    {"file": "a.py", "start": 2, "count": 2, "old": 2},
    {"file": "b.py", "start": 4, "count": 2, "old": 0},
    {"file": "c.py", "start": 1, "count": 5, "old": 0},
]


def _fixture():
    """Import tests/fixtures/overlay/hunk_fixture.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "hunk_fixture", Path(__file__).parent / "fixtures/overlay/hunk_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    assert gitdiff.open_in_editor(r, "a.py", 3, run=fake_run) == "text-editor"
    # -t (default text editor): plain `open` would execute .command/.workflow files.
    assert calls == [["open", "-t", str((r / "a.py").resolve())]]


def test_git_argv_defuses_repo_config(repo, monkeypatch):
    r, merge = repo
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="abc123\n")

    monkeypatch.setattr(gitdiff.subprocess, "run", fake_run)
    assert gitdiff.last_merge(r) == "abc123"
    gitdiff.hunks(r, merge)
    gitdiff.diff_text(r, merge)
    prefix = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
              "-c", "diff.external=", "-c", "core.pager=cat", "-c", "log.showSignature=false"]
    assert all(argv[:12] == prefix for argv in calls)
    # The diff calls also refuse external diff drivers and textconv filters.
    for argv in calls[1:]:
        assert "--no-ext-diff" in argv and "--no-textconv" in argv


def test_diff_text_on_repo(repo):
    r, merge = repo
    text = gitdiff.diff_text(r, merge)
    assert "diff --git" in text and "alpha3 edited" in text
    assert gitdiff.diff_text(r, merge, limit=10) == text[:10]
    assert gitdiff.diff_text(r / "nope", merge) == ""


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


def test_has_filters_only_for_repo_local_filters(repo):
    r, _ = repo
    assert gitdiff.has_filters(r) is False
    _git(r, "config", "--local", "filter.evil.clean", "touch pwned")
    assert gitdiff.has_filters(r) is True


def test_has_filters_catches_worktree_config(repo, tmp_path, monkeypatch):
    r, _ = repo
    _git(r, "config", "extensions.worktreeConfig", "true")
    _git(r, "config", "--worktree", "filter.sneaky.smudge", "touch pwned")
    assert gitdiff.has_filters(r) is True


def test_has_filters_ignores_global_filters(repo, tmp_path, monkeypatch):
    r, _ = repo
    glob = tmp_path / "global.gitconfig"
    glob.write_text("[filter \"lfs\"]\n\tclean = git-lfs clean -- %f\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    assert gitdiff.has_filters(r) is False


def test_open_in_editor_no_fallback_and_known_editors_only(repo):
    """The agent tier's scroll: `cursor`/`code` only, and with fallback=False nothing else opens."""
    r, _ = repo
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise FileNotFoundError

    assert gitdiff.open_in_editor(r, "a.py", 3, run=fake_run, editor="cursor", fallback=False) == "none"
    assert calls == [["cursor", "--goto", f"{(r / 'a.py').resolve()}:3"]]
    with pytest.raises(ValueError):
        gitdiff.open_in_editor(r, "a.py", 3, run=fake_run, editor="sh", fallback=False)
    with pytest.raises(ValueError):
        gitdiff.open_in_editor(r, "../x.py", 3, run=fake_run, editor="code", fallback=False)


def test_open_in_editor_times_out_and_skips_colon_names(repo):
    """R5 design 1+2: a hung editor CLI is cut off after a short timeout (no second editor opens), and a
    file named `a:9` is never passed to --goto (VS Code would read :9 as a line and open `a`)."""
    r, _ = repo
    calls = []

    def hung(cmd, **kwargs):
        calls.append((cmd, kwargs.get("timeout")))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    assert gitdiff.open_in_editor(r, "a.py", 3, run=hung) == "none"
    assert len(calls) == 1 and calls[0][1] == gitdiff._EDITOR_TIMEOUT <= 3

    (r / "a:9").write_text("x")
    calls.clear()

    def ok(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    assert gitdiff.open_in_editor(r, "a:9", 3, run=ok, fallback=False) == "none"
    assert calls == []


def test_hunks_carry_old_count_and_labels(tmp_path):
    truth = _fixture().build(tmp_path / "r")
    hs = [h for h in gitdiff.hunks(tmp_path / "r", truth["merge"]) if h["file"] == "sample.py"]
    assert len(hs) == 3
    assert all("old" in h for h in hs)
    assert [(h["start"], h["start"] + max(h["count"], 1) - 1) for h in hs] == [
        (t["start"], t["end"]) for t in truth["hunks"]]
    labels = [gitdiff.hunk_label(h, i + 1, 3) for i, h in enumerate(hs)]
    assert labels == ["1/3 +3", "2/3 changed", "3/3 removed here"]
    assert all(gitdiff.LABEL_RE.match(l) for l in labels)


def test_blame_subjects_and_hunk_diff(tmp_path):
    repo = tmp_path / "r"
    truth = _fixture().build(repo)
    assert gitdiff.blame_subjects(repo, "sample.py", 149, 149) == (["Rename the timeout flag"], False)
    assert gitdiff.blame_subjects(repo, "sample.py", 140, 142)[0] == ["Add the retry counter"]
    lines = (repo / "sample.py").read_text().splitlines()
    lines[9] = 'L010 = 10  # touched'  # uncommitted working-tree edit
    (repo / "sample.py").write_text("\n".join(lines) + "\n")
    assert gitdiff.blame_subjects(repo, "sample.py", 10, 10) == ([], True)
    text = gitdiff.hunk_diff(repo, truth["merge"], "sample.py", 149)
    assert text.startswith("@@") and "+L904 = 146" in text


def test_blame_never_runs_a_repo_textconv(tmp_path):
    """R1 security 1: a repo from an archive may set diff.<x>.textconv + .gitattributes; git blame
    applies textconv by default, so "why did this change?" must not run that program."""
    repo = tmp_path / "r"
    _fixture().build(repo)
    marker = tmp_path / "pwned"
    script = tmp_path / "conv.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n")
    script.chmod(0o755)
    _git(repo, "config", "diff.evil.textconv", str(script))
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    subjects, _ = gitdiff.blame_subjects(repo, "sample.py", 149, 149)
    assert not marker.exists()
    assert subjects == ["Rename the timeout flag"]
    assert gitdiff.has_filters(repo)  # the overlay refuses such a repo before any diff
    _git(repo, "config", "--unset", "diff.evil.textconv")
    _git(repo, "config", "diff.evil.command", str(script))
    assert gitdiff.has_filters(repo)


# --- since-the-merge turn (the service on top of the fixture repo) ---------------------------


def _wait(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _reset_overlay():
    overlay._history.clear()
    overlay._notes.clear()
    overlay._tainted.clear()
    overlay._card_ids.clear()
    overlay._pending.clear()
    overlay._hunks.clear()
    overlay.set_deps(None)
    yield
    overlay.set_deps(None)
    overlay._hunks.clear()


@pytest.fixture
def overlay_client(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(overlay, "TOKEN_FILE", token_file)
    return TestClient(make_app(), client=("127.0.0.1", 50000))


def test_since_merge_highlights_every_hunk_of_the_file_on_screen(overlay_client, tmp_path):
    """A git_diff turn with sample.py on screen: labelled boxes for each hunk, a scroll to the
    first one, an info card, and the hunk cursor stored; without an editor app nothing opens."""
    truth = _fixture().build(tmp_path / "home" / "r", extra=True)
    root = Path(truth["repo"])
    sample = root / "sample.py"
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="")

    overlay.set_deps(make_deps(run=fake_run))
    events = turn(overlay_client, "show the diff", conv_id="s1",
                  screen={"app": "Code", "document": str(sample)})
    assert [e for e in events if e["type"] == "lines"] == [
        {"type": "lines", "file": str(root / "sample.py"), "ranges": [[140, 142], [149, 149], [155, 155]],
         "labels": ["1/3 +3", "2/3 changed", "3/3 removed here"]}]
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert cards[-1]["title"] == "Changes in sample.py"
    assert cards[-1]["body"] == ["1/3 lines 140-142 added", "2/3 line 149 changed",
                                 "3/3 line 155 removed here", 'Say "next change" to jump.']
    assert _wait(lambda: calls == [["code", "--goto", f"{sample.resolve()}:140"]])  # hunk 1 only
    assert "2 files changed since the last merge. I've opened sample.py." in says(events)
    assert overlay._hunks["s1"].rel == "sample.py"  # sample.py on screen beat other.py's 12 lines

    calls.clear()  # a browser is not an editor: highlight and tell, but nothing opens
    overlay.set_deps(make_deps(run=fake_run))
    events = turn(overlay_client, "show the diff", conv_id="s1",
                  screen={"app": "Safari", "document": str(sample)})
    assert [e for e in events if e["type"] == "lines"]
    assert calls == []
    assert "2 files changed since the last merge. Showing the changes in sample.py." in says(events)

    def no_code(cmd, **kwargs):  # editor CLI missing: no fallback to `open`, ever
        calls.append(list(cmd))
        raise FileNotFoundError

    overlay.set_deps(make_deps(run=no_code))
    turn(overlay_client, "show the diff", conv_id="s1",
         screen={"app": "Code", "document": str(sample)})
    assert _wait(lambda: calls and calls[-1][0] == "code")
    assert all(c[0] != "open" for c in calls)


def _sign_head(repo: Path) -> None:
    """Rewrite HEAD with a fake gpgsig header: any signature check on it runs gpg.program."""
    body = _git(repo, "cat-file", "commit", "HEAD")
    head, msg = body.split("\n\n", 1)
    sig = "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEz\n -----END PGP SIGNATURE-----"
    proc = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, env=GIT_ENV,
                          input=f"{head}\n{sig}\n\n{msg}\n", capture_output=True, text=True, check=True)
    _git(repo, "update-ref", "HEAD", proc.stdout.strip())


def test_repo_gpg_program_never_runs(tmp_path, monkeypatch):
    """R2 security 1: a repo's own gpg.program (+ log.showSignature) runs on any signature check:
    the gate refuses the repo, the prefix turns showSignature off, and the agent's log/show are denied."""
    from mac.voice import agent_policy as ap

    home = tmp_path / "home"
    repo = home / "r"
    _fixture().build(repo)
    _sign_head(repo)
    marker = tmp_path / "pwned"
    script = tmp_path / "gpg.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o755)
    _git(repo, "config", "log.showSignature", "true")
    for key in ("gpg.program", "gpg.ssh.program", "gpg.x509.program"):
        _git(repo, "config", key, str(script))
        assert gitdiff.has_filters(repo), key
        _git(repo, "config", "--unset", key)
    _git(repo, "config", "gpg.program", str(script))
    ctx = ap.Ctx(home=str(home), vault=None, cwd=str(tmp_path))
    for cmd in ("log -1 --show-signature", "log --show-sig", "log --format=%G?", "show -s"):
        assert isinstance(ap.decide("Bash", {"command": f"git -C {repo} {cmd}"}, ctx), ap.Deny), cmd
    assert gitdiff.last_merge(repo)
    assert gitdiff._git(repo, "log", "-1", "--format=%H")
    assert not marker.exists()
    # The owner's own (global) gpg.program with a repo-local log.showSignature: the prefix keeps it off.
    _git(repo, "config", "--unset", "gpg.program")
    glob = tmp_path / "global.gitconfig"
    glob.write_text(f"[gpg]\n\tprogram = {script}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    assert not gitdiff.has_filters(repo)
    assert gitdiff.last_merge(repo) and gitdiff._git(repo, "show", "-s", "HEAD")
    assert not marker.exists()


# Repo-local keys whose value is a program git (or git-lfs) runs; every one makes the repo refused.
_PROGRAM_KEYS = ["gpg.program", "gpg.openpgp.program", "gpg.ssh.defaultKeyCommand", "core.sshCommand",
                 "core.askPass", "core.editor", "sequence.editor", "core.gitProxy", "core.alternateRefsCommand",
                 "credential.helper", "credential.https://x.example.helper", "remote.origin.uploadpack",
                 "remote.origin.receivepack", "remote.origin.vcs", "alias.st", "submodule.m.update",
                 "merge.evil.driver", "diff.evil.textconv", "diff.evil.command", "filter.evil.clean",
                 "interactive.diffFilter", "pager.log", "core.pager", "difftool.x.cmd", "mergetool.x.path",
                 "browser.x.cmd", "web.browser", "man.x.path", "man.viewer", "sendemail.smtpServer",
                 "sendemail.toCmd", "imap.tunnel", "tar.tgz.command", "trailer.t.cmd", "trailer.t.command",
                 "hook.pre.command", "gc.recentObjectsHook", "uploadpack.packObjectsHook", "http.proxy",
                 "lfs.customtransfer.x.path", "lfs.extension.x.clean", "lfs.extension.x.smudge",
                 # R3: programs git instaweb / git help -w / git cvsimport run (glm-cross R2)
                 "instaweb.httpd", "instaweb.browser", "help.browser", "cvsimport.cvsps",
                 # R3: a subsection name holding "=" must not cut the key short (security R2 LOW)
                 "remote.a=b.uploadpack", "credential.https://h/a=b.helper", "merge.a=b.driver"]
# Common repo-local keys that run nothing: they must not make a normal repo unusable.
_SAFE_KEYS = {"remote.origin.url": "https://example.invalid/a.git", "remote.origin.partialclonefilter": "blob:none",
              "branch.main.vscode-merge-base": "origin/main", "branch.main.remote": "origin",
              "core.hooksPath": ".husky", "pull.rebase": "true", "user.name": "x", "core.autocrlf": "input"}


@pytest.mark.parametrize("key", _PROGRAM_KEYS)
def test_has_filters_refuses_every_repo_program_key(repo, key):
    r, _ = repo
    _git(r, "config", key, "/tmp/never-run")
    assert gitdiff.has_filters(r) is True


def test_has_filters_ignores_unknown_scope(repo, monkeypatch):
    """R3 Mac blocker: Apple git lists Xcode's built-in gitconfig (credential.helper) with scope
    "unknown"; it is not the repo's, so it must not refuse every repo. Local/worktree still refuse."""
    r, _ = repo
    real = gitdiff._git
    seen = []

    def fake(repo_, *args):
        seen.append(args)
        out = real(repo_, *args)
        return "unknown\tcredential.helper\n" + out + "unknown\tcore.pager\n"

    monkeypatch.setattr(gitdiff, "_git", fake)
    assert gitdiff.has_filters(r) is False
    assert "--name-only" in seen[0]
    _git(r, "config", "credential.helper", "/tmp/never-run")
    assert gitdiff.has_filters(r) is True


def test_has_filters_fails_closed(repo, monkeypatch):
    """No listing (git failed or timed out) refuses the repo (security R2 INFO)."""
    r, _ = repo
    monkeypatch.setattr(gitdiff, "_git", lambda *a: None)
    assert gitdiff.has_filters(r) is True


def test_has_filters_includes_keep_the_includer_scope(repo, tmp_path):
    """Real git: a file pulled in by the repo's own include.path / includeIf is listed with the
    includer's scope (local / worktree), never as global/system/unknown, so the gate still refuses it."""
    r, _ = repo
    inc = tmp_path / "inc.gitconfig"
    inc.write_text("[core]\n\tsshCommand = /tmp/never-run\n")
    _git(r, "config", "include.path", str(inc))
    assert gitdiff.has_filters(r) is True
    _git(r, "config", "--unset", "include.path")
    assert gitdiff.has_filters(r) is False
    _git(r, "config", f"includeIf.gitdir:{r}/.path", str(inc))
    assert gitdiff.has_filters(r) is True
    _git(r, "config", "--unset", f"includeIf.gitdir:{r}/.path")
    _git(r, "config", "extensions.worktreeConfig", "true")
    _git(r, "config", "--worktree", "include.path", str(inc))
    listing = subprocess.run(["git", "config", "--includes", "--show-scope", "--name-only", "--list"],
                             cwd=r, capture_output=True, text=True, check=True).stdout
    assert "worktree\tcore.sshcommand" in listing.splitlines()
    assert gitdiff.has_filters(r) is True


def test_has_filters_keeps_plain_repo_keys(repo):
    r, _ = repo
    for key, value in _SAFE_KEYS.items():
        _git(r, "config", key, value)
    assert gitdiff.has_filters(r) is False
