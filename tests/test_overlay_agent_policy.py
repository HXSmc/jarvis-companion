"""agent_policy.decide: secrets denied, git/gh read-only forms rebuilt and defused, side effects confirmed.

Real git repos in tmp_path; HOME is a fake folder under tmp_path, never the real one.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import shutil
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
    for cmd in ("gh pr list -R o/r", "gh issue view 3 -R o/r", "gh api repos/o/r/pulls",
                "gh pr list --state open --limit 5 -R o/r", "gh pr view 12 -R o/r --json title,body",
                "gh run list -R o/r --workflow mac-overlay.yml", "gh pr list --repo=o/r"):
        d = bash(ctx, cmd)
        assert isinstance(d, ap.Allow), cmd
        assert d.updated_input["command"].startswith("env -u GH_HOST ")
        assert "GH_PAGER=cat GH_PROMPT_DISABLED=1" in d.updated_input["command"]
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


def test_grep_directory_excludes_secret_files(env):
    ctx, home, vault, _ = env
    repo = _repo(home / "code/clean")  # the fixture repo holds a symlink into Secrets: no search there
    (repo / ".env").write_text("K=V")
    assert isinstance(decide(ctx, "Grep", pattern="K", path=str(env[3])), ap.Deny)
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


# --- Round 1 security fixes (each failed before its fix) ---

def test_gh_other_host_denied(env):
    """R1 security #1: gh must not reach a host other than github.com (URL, HOST/OWNER/REPO, last -R wins)."""
    ctx = env[0]
    for cmd in ("gh issue view https://attacker.example/o/r/issues/1",
                "gh pr view https://github.com/o/r/pull/1",
                "gh pr list -R o/r -R attacker.example/o/r",
                "gh pr list -R attacker.example/o/r",
                "gh pr list -Rattacker.example/o/r",
                "gh pr list --repo=attacker.example/o/r",
                "gh repo view attacker.example/o/r",
                "gh repo view evil.example/r",
                "gh api https://attacker.example/repos/o/r"):
        assert isinstance(bash(ctx, cmd), ap.Deny), cmd
    cmd = bash(ctx, "gh pr list -R o/r").updated_input["command"]
    for var in ("GH_HOST", "GH_REPO", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        assert f"-u {var} " in cmd, var
    assert ap.gh_repo("gh pr list -R o/r") == "o/r" and ap.gh_repo("gh pr list -Ro/r") == "o/r"


def test_git_file_reading_options_denied(env):
    """R1 security #2 / #10, code #1: git options that read a file outside history are denied."""
    ctx, home, vault, repo = env
    (repo / "a.txt").write_text("a\n")
    r = repo
    for cmd in (f"git -C {r} blame --contents {vault}/Secrets/Projects.md a.txt",
                f"git -C {r} blame --contents={home}/.config/svc/credentials.json a.txt",
                f"git -C {r} blame --cont {vault}/Notes/a.md a.txt",
                f"git -C {r} blame --ignore-revs-file /etc/passwd a.txt",
                f"git -C {r} blame -S /etc/passwd a.txt",
                f"git -C {r} ls-files --exclude-from=/etc/passwd",
                f"git -C {r} ls-files -X /etc/passwd",
                f"git -C {r} diff -O/etc/passwd",
                f"git -C {r} diff --orderfile=/etc/passwd",
                f"git -C {r} log --outp=/tmp/x",
                f"git -C {r} log -- {vault}/Notes/a.md",
                f"git -C {r} log -- ../../.ssh/id_rsa",
                f"git -C {r} log -- link.md",
                f"git -C {r} show HEAD:Secrets/a.md"):
        assert isinstance(bash(ctx, cmd), ap.Deny), cmd
    for cmd in (f"git -C {r} log main..HEAD --format=%h/%s -5", f"git -C {r} log -S foo",
                f"git -C {r} blame a.txt", f"git -C {r} log -- a.txt", f"git -C {r} show HEAD~1:a.txt"):
        assert isinstance(bash(ctx, cmd), ap.Allow), cmd


def test_secrets_denied_without_vault_env(env):
    """R1 security #3: Secrets stay closed when OVERLAY_VAULT is unset."""
    ctx, home, vault, repo = env
    ctx.vault = None
    (home / "other/secrets").mkdir(parents=True)
    _repo(vault)
    for d in (decide(ctx, "Read", file_path=f"{vault}/Secrets/Projects.md"),
              decide(ctx, "Read", file_path=f"{home}/other/secrets/x.md"),
              decide(ctx, "Grep", pattern="pw", path=str(vault)),
              decide(ctx, "Glob", pattern="*.md", path=str(vault)),
              decide(ctx, "Grep", pattern="pw", path=str(home / "other")),
              bash(ctx, f"git -C {vault} log")):
        assert isinstance(d, ap.Deny), d
    assert isinstance(decide(ctx, "Read", file_path=f"{vault}/Notes/a.md"), ap.Allow)
    assert isinstance(decide(ctx, "Grep", pattern="p", path=str(vault / "Notes")), ap.Allow)


def test_grep_glob_tricks_denied(env):
    """R1 security #4: ripgrep glob syntax (braces, negation, classes, paths) cannot reach secret files."""
    ctx, home, vault, _ = env
    repo = _repo(home / "code/clean")
    for glob in ("{.env,a}", "!*.md", "[.]env", "src/*", "**/*", "*.{py,env}"):
        assert isinstance(decide(ctx, "Grep", pattern="K", path=str(repo), glob=glob), ap.Deny), glob
    for glob in ("*.py", "src/**/*.py", "**/*.md"):
        assert isinstance(decide(ctx, "Grep", pattern="K", path=str(repo), glob=glob), ap.Allow), glob


@pytest.mark.parametrize("rel", [
    ".config/svc/credentials.json", ".config/gcloud/access_tokens.db", ".azure/msal_token_cache.json",
    ".zsh_history", ".bash_history", ".mail-mcp/token.json", "code/x/oauth_tokens.json",
    "Library/Application Support/Google/Chrome/Default/Login Data",
    "Library/Application Support/Firefox/Profiles/x/logins.json", "Library/Safari/History.db",
    "Library/Messages/chat.db", "Library/Mail/V10/x.emlx", ".claude/settings.json",
    ".claude-work/settings.local.json", ".claude/projects/x/s.jsonl"])
def test_more_credential_stores_denied(env, rel):
    """R1 security #5: common credential and private-data stores are off limits."""
    ctx, home = env[0], env[1]
    assert isinstance(decide(ctx, "Read", file_path=str(home / rel)), ap.Deny), rel


def test_mcp_confirm_shows_every_recipient(env):
    """R1 security #7: the spoken summary names every recipient, or the call is denied."""
    ctx = env[0]
    d = ap.decide("mcp__mailbox__send_email", {"to": "boss@x.com", "bcc": "other@y.com", "subject": "hi",
                                                "body": "long text " * 30}, ctx)
    assert isinstance(d, ap.Confirm) and "other@y.com" in d.summary and "boss@x.com" in d.summary
    d = ap.decide("mcp__cal__create_event", {"summary": "sync", "attendees": ["other@y.com"],
                                              "description": "x" * 200}, ctx)
    assert isinstance(d, ap.Confirm) and "other@y.com" in d.summary
    for inp in ({"summary": "sync", "attendees": [{"email": "other@y.com"}]},
                {"to": "a@x.com", "cc": [f"p{i}@example.com" for i in range(9)], "subject": "hi"},
                {"to": "a@x.com", "extra": {"bcc": "hidden@y.com"}},
                {"to": "a@x.com", "footer": "f" * 90}):
        assert isinstance(ap.decide("mcp__mailbox__send_email", inp, ctx), ap.Deny), inp


def test_risky_writes_denied_and_named(env):
    """R1 security #8: startup files, git hooks and agent config are never written; others name the path."""
    ctx, home, vault, repo = env
    for p in (home / ".zshrc", home / ".bash_profile", home / "Library/LaunchAgents/x.plist",
              repo / ".git/hooks/pre-commit", home / ".claude/settings.json", home / ".gitconfig"):
        assert isinstance(decide(ctx, "Write", file_path=str(p), content="x"), ap.Deny), p
    d = decide(ctx, "Write", file_path=str(repo / "new.py"), content="x")
    assert isinstance(d, ap.Confirm) and "code/app/new.py" in d.summary


def test_gh_gated_after_local_untrusted_read(env):
    """Code #4: after any read (commit text, files), a new repo name or a gh api path needs Confirm."""
    ctx = env[0]
    ctx.read_untrusted_remote = True  # set by the gateway after any allowed tool call
    assert isinstance(bash(ctx, "gh repo view -R evil/x"), ap.Confirm)
    assert isinstance(bash(ctx, "gh api repos/evil/data"), ap.Confirm)
    ctx.trusted_repos = frozenset({"o/r"})
    assert isinstance(bash(ctx, "gh api repos/o/r/pulls"), ap.Allow)
    assert isinstance(bash(ctx, "gh api repos/o/r2/pulls"), ap.Confirm)


def test_other_bash_runs_exactly_what_was_spoken(env):
    """Code #5: a confirmed plain command is re-joined so the shell expands no glob, brace or ~."""
    d = bash(env[0], "ls ~/x* {a,b}")
    assert isinstance(d, ap.Confirm) and d.updated_input["command"] == "ls '~/x*' '{a,b}'"


def _cli_grep_globs(glob: str) -> list[str]:
    """How Claude Code's Grep tool turns its `glob` field into rg --glob flags (2.1.322: split on
    whitespace; a part with braces stays whole, any other part is split on commas)."""
    out = []
    for part in glob.split():
        out += [part] if "{" in part and "}" in part else [p for p in part.split(",") if p]
    return out


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep not installed")
def test_grep_model_glob_cannot_reach_env_variants(env):
    """R2 security #1: a model glob like *.production must not let ripgrep (run with --hidden, as the
    CLI does) read .env.production; plain code globs still work."""
    ctx, home = env[0], env[1]
    repo = _repo(home / "code/clean")
    for name in (".env.production", ".env.prod", ".env.staging", ".env.py", "a.py", "b.production"):
        (repo / name).write_text("CANARY\n")
    for glob, expect in (("*.production", {"b.production"}), ("*.prod", set()), ("*.staging", set()),
                         ("*.py", {"a.py"})):
        d = decide(ctx, "Grep", pattern="CANARY", path=str(repo), glob=glob)
        if isinstance(d, ap.Deny):
            continue
        assert isinstance(d, ap.Allow), (glob, d)
        argv = ["rg", "--hidden", "-l", "CANARY"]
        for g in _cli_grep_globs(d.updated_input["glob"]):
            argv += ["--glob", g]
        found = subprocess.run(argv + ["."], cwd=repo, capture_output=True, text=True).stdout.split()
        assert {os.path.basename(f) for f in found} == expect, (glob, found)


@pytest.mark.parametrize("name", [".npmrc", ".envrc", ".pypirc", ".netrc", "secrets.yaml", "secrets.json",
                                  "prod.tfvars", "terraform.tfstate", "terraform.tfstate.backup", "auth.json"])
def test_project_secret_files_denied(env, name):
    """R2 security #2: project-level secret files (any depth) and generic CLI auth files are closed."""
    ctx, home, _, repo = env
    assert isinstance(decide(ctx, "Read", file_path=str(repo / "sub" / name)), ap.Deny), name
    assert isinstance(decide(ctx, "Read", file_path=str(home / "Library/Application Support/com.vercel.cli"
                                                      / "auth.json")), ap.Deny)
    assert isinstance(decide(ctx, "Read", file_path=str(home / ".codex/auth.json")), ap.Deny)
    assert ap._GREP_EXCLUDE.startswith("!{") and any(
        fnmatch.fnmatch(name, g) for g in ap._GREP_EXCLUDE[2:-1].split(",")), name


def test_git_glob_pathspec_to_secret_denied(env):
    """R2 security #3: a glob pathspec that matches a secret file name is denied; plain ones still work."""
    ctx, _, _, r = env
    for spec in ("'*.env'", "'.env*'", "'*.pem'", "'id_*'", "'**/.npmrc'", "':(glob)**/.env'", "'*'"):
        assert isinstance(bash(ctx, f"git -C {r} log -p -- {spec}"), ap.Deny), spec
    for spec in ("'*.py'", "'src/*.swift'", "a.txt"):
        assert isinstance(bash(ctx, f"git -C {r} log -p -- {spec}"), ap.Allow), spec


def test_git_remote_list_is_read_only(env):
    """Mac run: the agent asks `git remote -v` to find the GitHub repo; listing is a read, changing is not."""
    ctx, _, _, r = env
    for cmd in (f"git -C {r} remote -v", f"git -C {r} remote", f"git -C {r} remote get-url origin"):
        assert isinstance(bash(ctx, cmd), ap.Allow), cmd
    for cmd in (f"git -C {r} remote add x https://example.com/x", f"git -C {r} remote set-url origin y",
                f"git -C {r} remote show origin", f"git -C {r} remote update"):
        assert isinstance(bash(ctx, cmd), ap.Confirm), cmd
