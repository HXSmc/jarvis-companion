"""Full auto for the voice agent's shell commands (owner decision B, jfollow10a).

The policy (agent_policy.decide) still decides Allow / Deny / Confirm. This module only says whether
a Bash Confirm may run without the spoken yes: never for the never-auto rows below, never once the
conversation took in text the owner did not say (unless the command is a plain look-up), never on a
barge-in turn or a follow-up the room-talk gate refused, and never with the owner's switch off.
Pure: no I/O except realpath/isdir and the one settings file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path

from mac.voice import agent, agent_policy

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path(agent.SUPPORT_DIR) / "voice-settings.json"  # read at call time (tests repoint it)
MAX_PARTS = 4

SUDO = frozenset({"sudo", "doas", "su", "pkexec", "login"})
RUNS_CODE = frozenset(agent_policy._WRAPPERS) | frozenset({
    "pip", "pipx", "uv", "uvx", "node", "npm", "npx", "pnpm", "yarn", "bun", "deno", "ruby", "gem",
    "perl", "php", "lua", "swift", "osascript", "awk", "gawk", "sed", "vim", "vi", "nvim", "nano", "emacs",
    "ed", "ex", "make", "cargo", "go", "brew", "docker", "open",
    # builder additions: other runners that execute their arguments or a script
    "tclsh", "expect", "jshell", "irb", "julia", "R", "Rscript", "java", "dotnet", "powershell", "pwsh",
    "watch", "parallel", "script", "tmux", "screen", "automator", "git-shell", "sqlite3",
})
RUNS_CODE_PREFIXES = ("python", "pip")  # python3, python3.12, pip3 ...
FIND_RUNS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf",
                       "-fls"})
DELETE = frozenset({"rm", "rmdir", "unlink", "shred", "srm", "trash", "truncate", "dd", "mv", "cp", "ln",
                    "install", "ditto", "tee", "tar", "unzip", "gunzip", "patch", "split", "mkfile",
                    # builder additions: other tools that overwrite or move files
                    "zip", "gzip", "bzip2", "xz", "csplit", "sips", "textutil", "plutil"})
SECRET = frozenset({"security", "printenv", "env", "set", "export", "pbpaste", "screencapture", "keychain",
                    # builder additions: other ways to dump the environment or the clipboard/screen
                    "declare", "typeset"})
RECURSIVE_READERS = frozenset({"grep", "rg", "ag", "ack", "cat", "head", "tail", "less", "strings", "du",
                               "egrep", "fgrep", "zgrep", "more"})
RECURSE_BY_DEFAULT = frozenset({"rg", "ag", "ack"})
NETWORK = frozenset({"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet",
                     "mail", "mailx", "sendmail", "msmtp", "shortcuts", "gh",
                     # builder additions: other senders
                     "ping", "dig", "nslookup", "host", "socat"})
GIT_AUTO = frozenset({"add", "commit", "fetch", "stash"})
STASH_AUTO = frozenset({"push", "list", "show"})
GUARD = frozenset({"launchctl", "defaults", "kill", "killall", "pkill", "tccutil", "csrutil", "spctl",
                   "systemsetup", "scutil", "networksetup", "pmset", "chmod", "chown", "chflags", "xattr",
                   "crontab", "at", "shutdown", "reboot", "halt", "diskutil",
                   # builder additions: login items, privileges, disks, firmware
                   "sfltool", "dscl", "nvram", "bless", "fdesetup", "softwareupdate", "systemextensionsctl",
                   "kextload", "kextunload", "mount", "umount", "hdiutil",
                   # builder addition: a cd moves where every later relative path lands
                   "cd", "pushd", "popd"})
GUARD_HOME = ("Library/Application Support/JarvisOverlay", "Library/LaunchAgents")
GUARD_ABS = ("/Library/LaunchDaemons", "/Library/LaunchAgents")

# Hub answer H1: plain look-ups that still run auto after the conversation took in untrusted text.
# The builder may not add entries.
READ_ONLY = frozenset({"date", "uptime", "df", "du", "memory_pressure", "vm_stat", "sw_vers", "uname",
                       "whoami", "hostname", "pwd", "echo", "ls", "cat", "head", "tail", "wc", "stat", "file",
                       "which"})
# The look-ups among them that print no file names or file text: running one leaves the mark unset.
QUIET = frozenset({"date", "uptime", "df", "memory_pressure", "vm_stat", "sw_vers", "uname", "whoami",
                   "hostname", "pwd", "echo"})

_NAME_RE = re.compile(r"[A-Za-z0-9._+-]{1,32}")
_PS_ENV_RE = re.compile(r"-?[A-Za-z]*[eE][A-Za-z]*")  # ps e / ps -E / ps auxe: may print environments


def _parts(command) -> list[list[str]] | None:
    """argv per ` && ` part; None when it cannot be parsed plainly (fail closed)."""
    if not isinstance(command, str) or not command.strip():
        return None
    parts = command.split(" && ")
    if len(parts) > MAX_PARTS or any(agent_policy._METACHARS & set(p) for p in parts):
        return None
    try:
        argvs = [shlex.split(p) for p in parts]
    except ValueError:
        return None
    return argvs if all(argvs) else None


def _base(argv: list[str]) -> str:
    return os.path.basename(argv[0])


def cmd_name(command) -> str:
    """The program's basename for the debug row (no arguments, no path), else a fixed word."""
    try:
        argv = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return "unparsed"
    if not argv:
        return "unparsed"
    name = _base(argv)
    return name if _NAME_RE.fullmatch(name) else "other"


def _values(argv: list[str]) -> list[str]:
    """Every argument and every --opt=value value: the strings that may name a file."""
    out = []
    for a in argv[1:]:
        out.append(a)
        if a.startswith("-") and "=" in a:
            out.append(a.split("=", 1)[1])
    return out


def _is_path(tok: str) -> bool:
    return "/" in tok or tok.startswith(("~", "."))


def _resolve(tok: str, ctx: agent_policy.Ctx, base: str | None = None) -> str:
    p = os.path.expanduser(tok.replace("~", ctx.home, 1) if tok.startswith("~") else tok)
    if not os.path.isabs(p):
        p = os.path.join(base if base is not None else (ctx.cwd or "."), p)
    return os.path.realpath(p)


def _places(tok: str, ctx: agent_policy.Ctx) -> list[str]:
    """Where a path may point: relative ones against the scratch dir AND home, since the shell's
    working directory is not ours to know once any cd ran (fail closed)."""
    if os.path.isabs(os.path.expanduser(tok)) or tok.startswith("~"):
        return [_resolve(tok, ctx)]
    return [_resolve(tok, ctx), _resolve(tok, ctx, ctx.home)]


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _git_sub(argv: list[str]) -> tuple[str, list[str]]:
    """(subcommand, its arguments) of a git argv, past -C <dir> / -c <k=v> and other options."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "-c"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, argv[i + 1:]
    return "", []


def _one(argv: list[str], ctx: agent_policy.Ctx) -> str:
    """The first never-auto code for one argv, or "ok"."""
    name = _base(argv)
    args = argv[1:]
    values = _values(argv)
    if name in SUDO:
        return "sudo"
    if (name in RUNS_CODE or name.startswith(RUNS_CODE_PREFIXES)
            or (name == "find" and any(a in FIND_RUNS for a in args))):
        return "runs_code"
    guard_roots = [os.path.realpath(os.path.join(ctx.home, d)) for d in GUARD_HOME]
    guard_roots += [os.path.realpath(str(agent.SUPPORT_DIR)), os.path.realpath(str(SETTINGS_FILE.parent))]
    guard_roots += [os.path.realpath(d) for d in GUARD_ABS]
    if name in GUARD or any(_is_path(v) and any(_under(_resolve(v, ctx), r) for r in guard_roots)
                            for v in values):
        return "guard"
    if name in NETWORK:
        return "network"
    if name == "git":
        sub, rest = _git_sub(argv)
        if sub == "push":
            return "network"
        if sub not in GIT_AUTO or (sub == "stash" and rest and rest[0] not in STASH_AUTO):
            return "git"
    if name in DELETE:
        scratch = os.path.realpath(ctx.cwd) if ctx.cwd else None
        targets = [a for a in args if not a.startswith("-")]
        # Absolute paths in the agent's own scratch dir only, never a shared one like /tmp (spec flag
        # c), and never a relative path: after a cd it lands elsewhere.
        if scratch is None or not targets or not all(
                os.path.isabs(os.path.expanduser(t)) and _under(_resolve(t, ctx), scratch) for t in targets):
            return "delete"
    if name in SECRET or (name == "ps" and any(_PS_ENV_RE.fullmatch(a) for a in args)):
        return "secret"
    for v in values:
        if agent_policy._secret_name(os.path.basename(v.rstrip("/")) or v):
            return "secret"
        if _is_path(v):
            for resolved in _places(v, ctx):
                problem = agent_policy._path_problem(v, resolved, ctx, search=False)
                if problem and agent_policy.reason_code(problem) in ("secret", "vault"):
                    return "secret"
    if name in RECURSIVE_READERS:
        recursive = name in RECURSE_BY_DEFAULT or any(
            a == "--recursive" or (a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a))
            for a in args)
        if recursive:
            dirs = [a for a in args if not a.startswith("-")]
            if name not in ("cat", "head", "tail", "less", "more", "strings", "du") and len(dirs) < 2:
                dirs.append(".")  # a searcher with only its pattern searches the working directory
            for d in dirs:
                for resolved in _places(d, ctx):
                    if os.path.isdir(resolved) and agent_policy._path_problem(d, resolved, ctx, search=True):
                        return "secret"
    return "ok"


def classify(command, ctx: agent_policy.Ctx) -> tuple[str, str]:
    """("auto", "ok") or ("asked", code) for the raw tool_input command. Argv parse, never a regex."""
    argvs = _parts(command)
    if argvs is None:
        return "asked", "parse"
    for argv in argvs:
        code = _one(argv, ctx)
        if code != "ok":
            return "asked", code
    return "auto", "ok"


def read_only(command) -> bool:
    """Every part is a plain look-up (READ_ONLY): may run auto after untrusted text."""
    argvs = _parts(command)
    return argvs is not None and all(_base(a) in READ_ONLY for a in argvs)


def marks(command) -> bool:
    """Running this command lets untrusted text in (its output may carry file names or text)."""
    argvs = _parts(command)
    return argvs is None or not all(_base(a) in QUIET for a in argvs)


def verdict(command, ctx: agent_policy.Ctx, *, tainted: bool, barge: bool, gate_ok: bool) -> tuple[str, str]:
    """("auto", "ok") or ("asked", reason). Order: off, never-auto row, untrusted, barge, follow-up gate."""
    if not auto_on():
        return "asked", "off"
    decision, code = classify(command, ctx)
    if decision != "auto":
        return decision, code
    if tainted and not read_only(command):
        return "asked", "untrusted"
    return gate(barge=barge, gate_ok=gate_ok)


def gate(*, barge: bool, gate_ok: bool) -> tuple[str, str]:
    """The room-talk part of the order: a barge-in or a follow-up the level gate refused asks."""
    if barge:
        return "asked", "barge"
    if not gate_ok:
        return "asked", "followup"
    return "auto", "ok"


# --- the owner's switch: one 0600 JSON file in the support dir ---

def _read() -> dict | None:
    """The settings dict; {} when the file is missing; None when it cannot be trusted."""
    path = SETTINGS_FILE
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    if stat.S_IMODE(st.st_mode) & 0o077:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def auto_on() -> bool:
    """Read on every call (no cache). Missing file = ON (the owner's default); damaged = OFF."""
    data = _read()
    if data is None:
        logger.warning("voice settings unreadable or too open: commands ask first")
        return False
    value = data.get("agent_auto", True)
    if not isinstance(value, bool):
        logger.warning("voice settings agent_auto is not true/false: commands ask first")
        return False
    return value


def set_auto(on: bool) -> bool:
    """Write {"agent_auto": on} atomically (dir 0700, file 0600), keeping other keys. False on failure."""
    path = SETTINGS_FILE
    data = _read() or {}
    data["agent_auto"] = bool(on)
    tmp = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".voice-settings.")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False
