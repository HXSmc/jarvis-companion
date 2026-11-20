"""jauto: simple commands run without a spoken yes; only dangerous ones ask.

`open` of documents, plain folders and web pages in an allowlisted app, shell no-ops and path printers
run auto; `open` of anything that can run code asks. A long plain command that would run auto is no
longer refused for length. Taint (except path-form open, hub Q4), barge-in, the follow-up gate and the
switch are unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from mac.voice import acks, agent_policy, auto_policy  # noqa: E402
from test_overlay_agent import Clock, client, reset, setup  # noqa: F401,E402
from test_overlay_jfollow10a import (  # noqa: F401,E402
    RUN_LINE, _confirm_card, _rows, _short_home, auto_on, bash, fresh_auto_state, run_agent)
from test_overlay_service import says  # noqa: E402

OK = ("auto", "ok")
RC = ("asked", "runs_code")
OUT = ("asked", "outside")
OWNER_NOTE = "Desktop/ObsidianVault/Brain/Research/llm-switch-local-vs-agents-benchmark-2026-11-18.md"
DOCS = ["a.doc", "a.pdf", "a.md", "b.md", "a.txt", "a.png", "x.command", "x.tool", "x.terminal", "x.scpt", "x.sh", "x.pkg",
        "x.dmg", "x.html", "x.svg", "x.webloc", "x.inetloc", "x.url", "x.fileloc", ".env", "k.pem"]


@pytest.fixture
def H(tmp_path):
    home = tmp_path / "h"
    docs = home / "Documents"
    docs.mkdir(parents=True)
    for name in DOCS:
        (docs / name).write_text("x")
    (docs / "tool").write_text("x")
    (docs / "tool").chmod(0o755)
    (docs / "run.md").write_text("x")
    (docs / "run.md").chmod(0o755)
    (docs / "X.app").mkdir()
    (docs / "Thing.workflow").mkdir()
    (docs / "Plain" / "Contents").mkdir(parents=True)
    (docs / "l.md").symlink_to("/etc/hosts")
    (home / "Library" / "Prefs").mkdir(parents=True)
    (home / "Library" / "Prefs" / "a.txt").write_text("x")
    (home / "Library" / "LaunchAgents").mkdir()
    (home / "Library" / "LaunchAgents" / "x.plist").write_text("x")
    (home / ".config").mkdir()
    (home / ".config" / "a.txt").write_text("x")
    vault = home / "Desktop" / "ObsidianVault"
    (vault / ".git").mkdir(parents=True)
    (vault / ".git" / "config").write_text("x")
    (vault / "Secrets").mkdir()
    (vault / "Secrets" / "a.md").write_text("x")
    (home / OWNER_NOTE).parent.mkdir(parents=True)
    (home / OWNER_NOTE).write_text("note")
    return home


@pytest.fixture
def ctx(H, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return agent_policy.Ctx(home=str(H), vault=str(H / "Desktop" / "ObsidianVault"), cwd=str(scratch))


def fill(command: str, H) -> str:
    return command.replace("H/", f"{H}/")


def v(command, ctx, tainted=False, barge=False, gate_ok=True):
    return auto_policy.verdict(command, ctx, tainted=tainted, barge=barge, gate_ok=gate_ok)


def _switch_off():
    path = auto_policy.SETTINGS_FILE
    path.write_text(json.dumps({"agent_auto": False}))
    os.chmod(path, 0o600)


# --- criteria 1-3: the owner's cases and the plain table ----------------------------------

def test_owner_open_runs(H, ctx, auto_on):
    command = f"open {H / OWNER_NOTE}"
    assert v(command, ctx) == OK
    assert isinstance(agent_policy.decide("Bash", {"command": command}, ctx), agent_policy.Confirm)
    assert auto_policy.marks(command) is False


def test_true_runs(ctx, auto_on):
    assert v("true", ctx) == OK
    assert auto_policy.marks("true") is False
    assert auto_policy.cmd_name("true") == "true"


PLAIN_ROWS = ["true", "false", ": x", "printf hi", "test -e H/Documents/a.md", "[ -e H/Documents/a.md ]",
              "basename H/Documents/a.md", "dirname H/Documents/a.md", "realpath H/Documents/a.md",
              "readlink H/Documents/l.md", "command -v ls", "command -V pwd"]


@pytest.mark.parametrize("command", PLAIN_ROWS)
def test_plain_table(command, H, ctx, auto_on):
    assert v(fill(command, H), ctx) == OK
    assert auto_policy.PLAIN == frozenset({"true", "false", ":", "printf", "test", "[", "basename", "dirname",
                                           "realpath", "readlink"})
    assert auto_policy.PLAIN & (auto_policy.READ_ONLY | auto_policy.QUIET) == set()
    for bad in ("command ls", "command -p ls", "command -v", "command -v -p"):
        assert v(bad, ctx) == RC, bad


# --- criteria 4-6: open rows ---------------------------------------------------------------

AUTO_ROWS = ["open H/Documents/a.pdf", "open H/Documents", "open https://example.com/x", "open HTTP://example.com",
             "open -R H/Documents/a.md", "open -g H/Documents/a.md", "open -e H/Documents/a.txt",
             "open -a Preview H/Documents/a.png", "open -a 'Google Chrome' https://example.com", "open -a Safari",
             "open -a Obsidian.app H/Documents/a.md", "open H/Documents/a.md H/Documents/b.md"]


@pytest.mark.parametrize("command", AUTO_ROWS)
def test_open_auto_rows(command, H, ctx, auto_on):
    assert v(fill(command, H), ctx) == OK


ASK_ROWS = [(f"open H/Documents/x.{e}", RC) for e in (
    "command", "tool", "terminal", "scpt", "sh", "pkg", "dmg", "html", "svg", "webloc", "inetloc", "url", "fileloc")] + [
    ("open H/Documents/tool", RC), ("open H/Documents/run.md", RC), ("open H/Documents/X.app", RC),
    ("open H/Documents/Thing.workflow", RC), ("open H/Documents/Plain", RC), ("open file:///etc/hosts", RC),
    ("open 'obsidian://new?vault=x'", RC), ("open vscode://file/x", RC), ("open ssh://h", RC), ("open smb://h/s", RC),
    ("open x-apple.systempreferences:com.apple.preference.security", RC), ("open -a Terminal H/Documents/a.md", RC),
    ("open -a iTerm", RC), ("open -a 'Script Editor' H/Documents/a.md", RC), ("open -a Automator", RC),
    ("open -a Shortcuts", RC), ("open -a /Applications/Safari.app", RC), ("open -aSafari H/Documents/a.md", RC),
    ("open --args x", RC), ("open -a Safari --args --x", RC), ("open -n -a Safari", RC), ("open -f", RC),
    ("open -W H/Documents/a.md", RC), ("open -b com.apple.Terminal", RC), ("open -u https://x", RC),
    ("open --stdout H/Documents/o.txt -a Safari", RC), ("open --env A=1 -a Safari", RC),
    ("open -- H/Documents/a.md", RC), ("open", RC), ("open H/Documents/a.md" + " H/Documents/a.md" * 4, RC),
    # spec advice 6: -a with no value, -e / -g with nothing to open, a second -a, a cluster
    ("open -a", RC), ("open -e", RC), ("open -g", RC), ("open -a Safari -a Preview", RC), ("open -gR H/Documents", RC),
    # review round 1: a non-ASCII app name, an option after an operand
    ("open -a ſafari", RC), ("open -a Keynote", RC), ("open H/Documents/a.md -g", RC),
    ("open H/Documents/a.md -a Safari", RC), ("open https://example.com -R", RC),
    ("open 'https://[x'", RC), ("open 'https://[x]'", RC),
    ("open Documents/a.md", OUT), ("open '~/Documents/a.md'", OUT), ("open /etc/hosts", OUT),
    ("open H/Library/Prefs/a.txt", OUT), ("open H/.config/a.txt", OUT), ("open H/Documents/missing.md", OUT),
    ("open H/Documents/l.md", OUT), ("open /net/host/a.md", ("asked", "network")),
]


@pytest.mark.parametrize("command,expected", ASK_ROWS)
def test_open_ask_rows(command, expected, H, ctx, auto_on):
    assert v(fill(command, H), ctx) == expected


@pytest.mark.parametrize("command", ["open H/Documents/.env", "open H/Documents/k.pem",
                                     "open H/Library/LaunchAgents/x.plist",
                                     "open H/Desktop/ObsidianVault/Secrets/a.md",
                                     "open H/Desktop/ObsidianVault/.git/config"])
def test_open_guard_and_secret_paths(command, H, ctx, auto_on):
    assert v(fill(command, H), ctx)[0] == "asked"
    assert v(fill(command, H), ctx, tainted=True)[0] == "asked"


def test_open_app_bundle_checked_on_the_resolved_path(H, ctx, auto_on):
    """A plain-named link to a bundle, or to a document inside one, still asks."""
    inner = H / "Documents" / "X.app" / "Contents" / "a.md"
    inner.parent.mkdir(parents=True)
    inner.write_text("x")
    (H / "Documents" / "plainlink").symlink_to(H / "Documents" / "X.app")
    (H / "Documents" / "doclink.md").symlink_to(inner)
    assert v(f"open {H}/Documents/plainlink", ctx) == RC
    assert v(f"open {H}/Documents/doclink.md", ctx) == RC


# --- criterion 7 + hub Q4: states ------------------------------------------------------------

def test_states_unchanged(H, ctx, auto_on):
    owner = f"open {H / OWNER_NOTE}"
    for command in (owner, "open https://example.com", "true", "printf hi"):
        tainted = OK if command == owner else ("asked", "untrusted")
        assert v(command, ctx, tainted=True) == tainted, command
        assert v(command, ctx, barge=True) == ("asked", "barge"), command
        assert v(command, ctx, gate_ok=False) == ("asked", "followup"), command
    _switch_off()
    for command in (owner, "open https://example.com", "true", "printf hi"):
        assert v(command, ctx) == ("asked", "off"), command


def test_open_path_tainted_runs(H, ctx, auto_on):
    assert v(fill("open H/Documents/a.pdf", H), ctx, tainted=True) == OK
    assert v(fill("open -R H/Documents/a.md H/Documents", H), ctx, tainted=True) == OK
    assert v(fill("cat H/Documents/a.md && open H/Documents/b.md", H), ctx, tainted=True) == OK
    assert v(fill("open H/Documents/a.pdf", H), ctx, tainted=True, barge=True) == ("asked", "barge")
    assert v(fill("open H/Documents/a.pdf", H), ctx, tainted=True, gate_ok=False) == ("asked", "followup")


def test_open_url_tainted_asks(H, ctx, auto_on):
    untrusted = ("asked", "untrusted")
    assert v("open https://example.com", ctx, tainted=True) == untrusted
    assert v(fill("open H/Documents/a.md https://example.com", H), ctx, tainted=True) == untrusted
    assert v("open -a Safari", ctx, tainted=True) == untrusted  # no operand: not the path form
    assert v(fill("open -a Preview H/Documents/a.pdf", H), ctx, tainted=True) == OK  # jauto2: viewer -a, a file
    assert v(fill("open -e H/Documents/a.txt", H), ctx, tainted=True) == OK  # jauto2: -e (TextEdit), a file
    assert v(fill("printf hi && open H/Documents/b.md", H), ctx, tainted=True) == untrusted
    assert v(fill("curl https://x && open H/Documents/b.md", H), ctx, tainted=True) == ("asked", "network")


# --- criterion 8: dangerous classes ----------------------------------------------------------

DANGER = [("rm H/a", "delete"), ("sudo ls", "sudo"), ("python3 -c 1", "runs_code"), ("bash x.sh", "runs_code"),
          ("make", "runs_code"), ("brew install x", "runs_code"), ("docker ps", "runs_code"),
          ("curl https://x", "network"), ("launchctl list", "guard"), ("defaults read x", "guard"),
          ("kill 1", "guard"), ("security find-generic-password", "secret"), ("git -C H/r push", "network"),
          ("cp H/a /etc/x", "outside"), ("tool-x --check", "unknown"), ("osascript -e 1", "runs_code"),
          ("xargs rm", "runs_code"), ("env X=1 ls", "runs_code")]


@pytest.mark.parametrize("command,code", DANGER)
def test_dangerous_classes_still_ask(command, code, H, ctx, auto_on):
    assert v(fill(command, H), ctx) == ("asked", code)


# --- criterion 9: length rule ---------------------------------------------------------------

def _deep(H, name: str) -> Path:
    d = H / "Documents" / ("folder-with-a-long-name/" * 6)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("x")
    return d / name


def test_long_command_rules(H, ctx, auto_on, tmp_path):
    doc = _deep(H, "a.md")
    long_open = f"open {doc}"
    assert len(f"run {long_open}") > 120
    got = agent_policy.decide("Bash", {"command": long_open}, ctx)
    assert isinstance(got, agent_policy.Confirm) and got.spoken_ok is False  # (a)
    assert v(long_open, ctx) == OK
    got = agent_policy.decide("Bash", {"command": f"rm {doc}"}, ctx)  # (b)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"
    repo = H / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    got = agent_policy.decide("Bash", {"command": f"git -C {repo} commit -m '{'x' * 130}'"}, ctx)  # (d)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"
    ctx.read_untrusted_remote = True
    got = agent_policy.decide("Bash", {"command": long_open}, ctx)
    assert isinstance(got, agent_policy.Confirm) and got.spoken_ok is False
    got = agent_policy.decide("Bash", {"command": f"open {_deep(H, 'x.command')}"}, ctx)  # (e)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"
    got = agent_policy.decide("Bash", {"command": "printf hi"}, ctx)  # (f)
    assert isinstance(got, agent_policy.Confirm) and got.spoken_ok is True
    _switch_off()
    got = agent_policy.decide("Bash", {"command": long_open}, ctx)  # (c)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"


# --- review round 1 (security findings 1-6) ----------------------------------------------------

def test_r1_long_non_exempt_commands_refused(H, ctx, auto_on):
    """Finding 1: the long-summary exception covers only taint-exempt commands with an uncut summary."""
    public = H / "Public"
    public.mkdir()
    srcs = []
    for i in range(12):
        f = H / "Documents" / f"private-file-number-{i:02d}.txt"
        f.write_text("x")
        srcs.append(str(f))
    long_cp = f"cp {' '.join(srcs)} {public}/"
    assert v(long_cp, ctx) == OK and v(long_cp, ctx, tainted=True) == ("asked", "untrusted")
    long_url = "open https://example.com/?d=" + "x" * 130
    long_mv = f"mv {srcs[0]} {public}/{'deep-name-' * 12}.txt"
    for command in (long_cp, long_url, long_mv):
        got = agent_policy.decide("Bash", {"command": command}, ctx)
        assert isinstance(got, agent_policy.Deny), command
        assert got.reason == "too long to confirm out loud; split it into smaller steps"
    long_open = f"open {_deep(H, 'n.md')}"  # the owner's case: a long path-form open is not refused
    got = agent_policy.decide("Bash", {"command": long_open}, ctx)
    assert isinstance(got, agent_policy.Confirm) and got.spoken_ok is False
    assert v(long_open, ctx, tainted=True) == OK


def test_r1_cut_summary_refused(ctx, auto_on, monkeypatch):
    """Finding 1: a summary cut at 10,000 characters never reaches a Confirm. macOS paths stop at 1024
    bytes, so no real file gets there: the classifier is stubbed to say auto and taint-exempt."""
    command = "open " + " ".join(["/" + "d" * 2600] * 4)
    monkeypatch.setattr(auto_policy, "classify", lambda c, x: OK)
    monkeypatch.setattr(auto_policy, "read_only", lambda c: True)
    got = agent_policy.decide("Bash", {"command": command}, ctx)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"
    short = "open /" + "d" * 200  # same stubs, uncut long summary: kept
    assert isinstance(agent_policy.decide("Bash", {"command": short}, ctx), agent_policy.Confirm)


def test_r1_legacy_office_not_taint_exempt(H, ctx, auto_on):
    """Finding 2: .doc/.xls/.ppt may hold macros: auto untainted, asked after untrusted text."""
    (H / "Documents" / "doclink.md").symlink_to(H / "Documents" / "a.doc")
    assert v(fill("open H/Documents/a.doc", H), ctx) == OK
    assert v(fill("open H/Documents/doclink.md", H), ctx) == OK
    for command in ("open H/Documents/a.doc", "open H/Documents/doclink.md", "open H/Documents/a.md H/Documents/a.doc"):
        assert v(fill(command, H), ctx, tainted=True) == ("asked", "untrusted"), command


@pytest.mark.parametrize("head", [b"book\0\0\0\0mark", b"alis\0\0\0\0"])
def test_r1_finder_alias_asks(head, H, ctx, auto_on):
    """Finding 3: a Finder alias / bookmark opens its target, which realpath cannot see."""
    (H / "Documents" / "report.pdf").write_bytes(head + b"rest")
    assert v(fill("open H/Documents/report.pdf", H), ctx) == RC


def test_r1_marks_only_allowlisted_open_is_quiet():
    """Finding 6: open forms outside the allowlist (-h, --stdout ...) may print text."""
    assert auto_policy.marks("open /x/a.md") is False
    assert auto_policy.marks("open -a Safari") is False
    for command in ("open -h", "open --stdout /x/o.txt -a Safari", "open /x/a.md -g", "open -a"):
        assert auto_policy.marks(command) is True, command


# --- criteria 10-11 ---------------------------------------------------------------------------

def test_import_orders():
    for mods in ("mac.voice.agent_policy, mac.voice.auto_policy", "mac.voice.auto_policy, mac.voice.agent_policy"):
        out = subprocess.run([sys.executable, "-c", f"import {mods}"], cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
    src = (ROOT / "mac" / "voice" / "agent_policy.py").read_text().splitlines()
    assert not [ln for ln in src if ln.startswith(("from mac.voice import", "import")) and "auto_policy" in ln]


def test_open_table_pins():
    assert auto_policy.OPEN_APPS == frozenset({
        "finder", "preview", "textedit", "safari", "google chrome", "firefox", "arc", "obsidian", "quicktime player",
        "music", "photos", "pages", "numbers", "keynote", "microsoft word", "microsoft excel", "microsoft powerpoint"})
    assert auto_policy.OPEN_DOCS == frozenset({
        "md", "markdown", "txt", "rtf", "pdf", "csv", "tsv", "png", "jpg", "jpeg", "gif", "heic", "heif", "webp",
        "tif", "tiff", "bmp", "mov", "mp4", "m4v", "mp3", "m4a", "wav", "aif", "aiff", "aac", "flac", "doc", "docx",
        "xls", "xlsx", "ppt", "pptx", "pages", "numbers", "epub"})
    assert auto_policy.NO_OUTPUT == frozenset({"open", "true", "false", ":"})
    assert "open" not in auto_policy.RUNS_CODE and "open" in auto_policy.KNOWN
    assert {"terminal", "iterm", "iterm2", "script editor", "automator", "shortcuts", "xcode",
            "installer"} & auto_policy.OPEN_APPS == set()
    assert {"html", "htm", "svg", "webloc", "command", "sh", "app", "pkg", "dmg", "scpt", "terminal", "tool",
            "workflow", "json", "plist", "key"} & auto_policy.OPEN_DOCS == set()


# --- criterion 12: through the gateway ------------------------------------------------------

def test_overlay_open_runs_auto(client, auto_on):
    home = Path.home()
    (home / "Documents").mkdir(exist_ok=True)
    (home / "Documents" / "jauto.md").write_text("x")
    (home / "Documents" / "jauto.command").write_text("x")
    pool, events = run_agent(client, bash(f"open {_short_home()}/Documents/jauto.md"), conv_id="ja1")
    assert _rows() == ["cmd=open decision=auto reason=ok"]
    assert [r["behavior"] for r in pool.made[0].responses] == ["allow"]
    assert says(events).count(acks.PHRASES["progress_run"]) == 1 and not _confirm_card(events)
    pool, events = run_agent(client, bash(f"open {_short_home()}/Documents/jauto.command"), conv_id="ja2")
    assert _rows()[-1] == "cmd=open decision=asked reason=runs_code"
    assert _confirm_card(events) and pool.made[0].responses == []
    assert RUN_LINE not in says(events)
