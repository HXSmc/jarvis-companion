"""agent_policy.decide: secrets denied, git/gh read-only forms rebuilt and defused, side effects confirmed.

Real git repos in tmp_path; HOME is a fake folder under tmp_path, never the real one.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from mac.voice import agent_policy as ap
from mac.voice import gitdiff


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    vault = home / "vault"
    for d in (vault / "Secrets", vault / "Notes", vault / "Projects", home / ".ssh", home / ".config/gh",
              home / ".claude", home / "Library/Application Support/JarvisOverlay", tmp_path / "scratch"):
        d.mkdir(parents=True, exist_ok=True)
    (vault / "Secrets/Projects.md").write_text("x")
    (vault / "Secrets/a.md").write_text("x")
    (vault / "Notes/a.md").write_text("pricing")
    repo = _repo(home / "code/app")
    (repo / ".env").write_text("K=V")
    os.symlink(vault / "Secrets/a.md", repo / "link.md")
    ctx = ap.Ctx(home=str(home), vault=str(vault), cwd=str(tmp_path / "scratch"))
    return ctx, home, vault, repo


def decide(ctx, tool, **inp):
    return ap.decide(tool, inp, ctx)


def bash(ctx, cmd):
    return ap.decide("Bash", {"command": cmd}, ctx)


def test_secrets_dir_denied(env):
    ctx, home, vault, repo = env
    for d in (decide(ctx, "Read", file_path=f"{vault}/Secrets/Projects.md"),
              decide(ctx, "Read", file_path=f"{vault}/secrets/x.md"),
              decide(ctx, "Read", file_path=str(repo / "link.md")),
              decide(ctx, "Grep", pattern="pw", path=str(vault)),
              decide(ctx, "Glob", pattern="*.md", path=str(vault))):
        assert isinstance(d, ap.Deny), d
    assert isinstance(decide(ctx, "Read", file_path=f"{vault}/Notes/a.md"), ap.Allow)
    reason = decide(ctx, "Grep", pattern="pw", path=str(vault)).reason
    assert "Notes" in reason and "Secrets," not in reason and not reason.endswith("Secrets")


def test_credential_paths_denied(env):
    ctx, home, vault, repo = env
    for p in (home / ".ssh/id_ed25519", home / ".config/gh/hosts.yml", repo / ".env",
              home / "Library/Application Support/JarvisOverlay/token", home / ".claude.json",
              home / ".claude/.credentials.json", f"{repo}/../../.ssh/id_rsa", "/etc/passwd"):
        assert isinstance(decide(ctx, "Read", file_path=str(p)), ap.Deny), p


def test_git_readonly_allowed_and_defused(env):
    ctx, home, vault, repo = env
    d = bash(ctx, f"git -C {repo} log -5 --stat")
    assert isinstance(d, ap.Allow)
    assert d.updated_input["command"].startswith(shlex.join(gitdiff._GIT_PREFIX))
    assert "--no-ext-diff" in d.updated_input["command"]
    assert isinstance(bash(ctx, f"git -C {repo} branch --list"), ap.Allow)


def test_git_injection_denied(env, tmp_path):
    ctx, home, vault, repo = env
    filtered = _repo(home / "code/filtered")
    subprocess.run(["git", "config", "filter.x.clean", "cat"], cwd=filtered, check=True)
    _repo(vault)
    r = repo
    for cmd in (f"git -C {r} log; rm -rf ~", f"git -C {r} log $(id)", f"git -C {r} log `id`",
                f"git -C {r} log --output=/tmp/x", f"git -C {r} diff --ext-diff",
                f"git -C {r} -c core.pager=sh log", f"git -C {r} log | sh", f"git -C {r} log\nrm x",
                "git -C /etc log", f"git -C {filtered} diff", f"git -C {r} show HEAD:.env",
                f"git -C {vault} log", f"git -C {r} diff --no-index /etc/passwd /dev/null"):
        assert isinstance(bash(ctx, cmd), ap.Deny), cmd
    for cmd in ("cat ~/.ssh/id_rsa", "rg secret ~"):
        assert not isinstance(bash(ctx, cmd), ap.Allow), cmd


def test_side_effects_need_confirm(env):
    ctx, home, vault, repo = env
    r = repo
    cases = [bash(ctx, c) for c in (f"git -C {r} commit -m x", f"git -C {r} push",
                                    f"git -C {r} add -A && git -C {r} commit -m wip && git -C {r} push",
                                    "gh pr merge 1", "gh pr create", "gh api -X POST repos/o/r/issues",
                                    "gh api repos/o/r/issues -f title=x")]
    cases += [decide(ctx, "Write", file_path=str(home / "code/app/new.py"), content="x"),
              decide(ctx, "Edit", file_path=str(home / "code/app/new.py"), old_string="a", new_string="b"),
              decide(ctx, "mcp__mailbox__delete_email", id="1")]
    for d in cases:
        assert isinstance(d, ap.Confirm), d
        assert len(d.summary) <= 120 and "<" not in d.summary and ">" not in d.summary
    assert "core.hooksPath=/dev/null" in cases[1].updated_input["command"]


def test_gh_readonly_allowed(env):
    ctx = env[0]
    for cmd in ("gh pr list -R o/r", "gh issue view 3 -R o/r", "gh api repos/o/r/pulls"):
        d = bash(ctx, cmd)
        assert isinstance(d, ap.Allow), cmd
        assert d.updated_input["command"].startswith("env GH_PAGER=cat GH_PROMPT_DISABLED=1")
    for cmd in ("gh pr view 1 --web", "gh api --hostname evil.example repos/x", "gh pr list -R 'o/r;x'"):
        assert isinstance(bash(ctx, cmd), ap.Deny), cmd


def test_unknown_tool_denied(env):
    ctx = env[0]
    for tool in ("WebFetch", "WebSearch", "Task", "Agent", "Frobnicate"):
        assert isinstance(ap.decide(tool, {"url": "https://x.example"}, ctx), ap.Deny), tool
    assert not isinstance(ap.decide("NotebookEdit", {"notebook_path": "/etc/x.ipynb"}, ctx), ap.Allow)


def test_mcp_read_only_list(env):
    ctx = env[0]
    ctx.read_only_mcp = frozenset({"mcp__mailbox__search_emails"})
    assert isinstance(ap.decide("mcp__mailbox__search_emails", {"query": "x"}, ctx), ap.Allow)
    d = ap.decide("mcp__mailbox__send_email", {"to": "a@example.com", "subject": "Hi"}, ctx)
    assert isinstance(d, ap.Confirm) and "a@example.com" in d.summary and "Hi" in d.summary
    assert isinstance(ap.decide("mcp__othermail__search_emails", {"query": "x"}, ctx), ap.Confirm)


def test_gh_api_gated_after_untrusted_read(env):
    ctx = env[0]
    assert isinstance(bash(ctx, "gh api repos/o/r/pulls"), ap.Allow)
    ctx.read_untrusted_remote = True
    assert isinstance(bash(ctx, "gh api repos/o/r/pulls"), ap.Confirm)
    ctx.trusted_repos = frozenset({"o/r"})
    assert isinstance(bash(ctx, "gh pr list -R o/r"), ap.Allow)
    assert isinstance(bash(ctx, "gh pr list -R x/y"), ap.Confirm)
    assert ap.reads_remote_text("Bash", {"command": "gh pr view 1 -R o/r"})
    assert not ap.reads_remote_text("Bash", {"command": "git -C r log"})
    assert ap.reads_remote_text("mcp__mailbox__search_emails", {})


def test_grep_directory_excludes_secret_files(env):
    ctx, home, vault, repo = env
    d = decide(ctx, "Grep", pattern="K", path=str(repo))
    assert isinstance(d, ap.Allow) and d.updated_input["glob"].startswith("!")
    assert isinstance(decide(ctx, "Grep", pattern="K", path=str(repo), glob="*.py"), ap.Allow)
    for glob in (".env*", "*", "../*"):
        assert isinstance(decide(ctx, "Grep", pattern="K", path=str(repo), glob=glob), ap.Deny), glob


def test_kind_and_log_label(env):
    ctx, home, vault, repo = env
    assert ap.kind_of("Bash", {"command": f"git -C {repo} log"}, ctx) == "git"
    assert ap.kind_of("Bash", {"command": "gh pr list"}, ctx) == "gh"
    assert ap.kind_of("Read", {"file_path": f"{vault}/Notes/a.md"}, ctx) == "notes"
    assert ap.kind_of("Read", {"file_path": str(repo / "a.py")}, ctx) == "file"
    assert ap.kind_of("mcp__mailbox__search_emails", {}, ctx) == "mail"
    assert ap.kind_of("mcp__work_calendar__list_events", {}, ctx) == "calendar"
    label = ap.log_label("Bash", {"command": "git -C /x/secret log --stat SECRET-ARG"})
    assert label == "Bash git log"
    assert ap.log_label("Bash", {"command": "cat /x/secret"}) == "Bash other"


def test_decide_never_raises(env):
    ctx = env[0]
    for tool, inp in (("Bash", {}), ("Bash", {"command": None}), ("Read", {"file_path": 5}), ("", {}),
                      ("Read", None), ("Grep", {"path": 5})):
        assert isinstance(ap.decide(tool, inp, ctx), ap.Deny), (tool, inp)
