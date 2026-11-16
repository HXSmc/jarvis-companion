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
    # round 1: stock macOS shells and exec wrappers (each runs another program), debuggers, dc (runs !cmd)
    "csh", "tcsh", "arch", "xcrun", "lockf", "apply", "ssh-agent", "sandbox-exec", "lldb", "gdb", "dc",
    # round 2: more stock wrappers (taskpolicy, leaks --atExit --, chroot run a program; man -P names one)
    "taskpolicy", "leaks", "chroot", "man",
})
# python3, python3.12, pip3, perl5.34, ruby3.2, ipython, pypy3, bash5.2, zsh5 ...
RUNS_CODE_PREFIXES = ("python", "pip", "perl", "ruby", "php", "tclsh", "ipython", "pypy", "jython",
                      "bash", "zsh", "ksh", "fish", "dash", "csh", "tcsh")
FIND_RUNS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf",
                       "-fls"})
DELETE = frozenset({"rm", "rmdir", "unlink", "shred", "srm", "trash", "truncate", "dd", "mv", "cp", "ln",
                    "install", "ditto", "tee", "tar", "unzip", "gunzip", "patch", "split", "mkfile",
                    # builder additions: other tools that overwrite or move files
                    "zip", "gzip", "bzip2", "xz", "csplit", "sips", "textutil", "plutil",
                    # round 1: archivers that extract over any path
                    "cpio", "pax",
                    # round 2: edits the plist named in its arguments
                    "PlistBuddy"})
# round 1: tools that write a file named by -o/--output (sort, base64, say, osacompile) or by their 2nd
# operand (uniq, xxd); that file counts as a delete target, with the same scratch-dir exception.
OUTPUT_OPT = frozenset({"sort", "base64", "say", "osacompile",
                        # round 2: tiffutil -out f; compilers -o f
                        "tiffutil", "cc", "gcc", "clang", "c++", "g++", "clang++", "swiftc"})
OUTPUT_OPERAND = frozenset({"uniq", "xxd", "afconvert"})
SECRET = frozenset({"security", "printenv", "env", "set", "export", "pbpaste", "screencapture", "keychain",
                    # builder additions: other ways to dump the environment or the clipboard/screen
                    "declare", "typeset",
                    # round 1: password stores and decryption
                    "op", "bw", "pass", "gpg"})
RECURSIVE_READERS = frozenset({"grep", "rg", "ag", "ack", "cat", "head", "tail", "less", "strings", "du",
                               "egrep", "fgrep", "zgrep", "more"})
RECURSE_BY_DEFAULT = frozenset({"rg", "ag", "ack"})
NETWORK = frozenset({"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet",
                     "mail", "mailx", "sendmail", "msmtp", "shortcuts", "gh",
                     # builder additions: other senders
                     "ping", "dig", "nslookup", "host", "socat",
                     # round 1: Apple's HTTP client, TLS client, other HTTP / cloud / VPN senders
                     "nscurl", "openssl", "http", "https", "xh", "aws", "gcloud", "rclone", "tailscale",
                     # round 2: more stock senders (mount_smbfs, mount_nfs ... by the mount_ prefix)
                     "ssh-keyscan", "whois", "traceroute", "traceroute6", "dns-sd", "tftp", "smbutil",
                     "ldapsearch", "lpr", "networkQuality"})
NETWORK_PREFIXES = ("mount_",)
# Round 2: the macOS automount points. Any look-up of a path under them contacts the named host, so a
# path there is a send (and is never resolved here). Compared case-folded: APFS is case-insensitive.
NET_ROOTS = ("/net", "/network", "/system/volumes/data/net", "/system/volumes/data/network")
DATA_VOLUME = "/system/volumes/data"  # compared case-folded, like NET_ROOTS
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


def _attached(a: str) -> str | None:
    """The path an option carries inside its own token: --opt=path, or -Xpath / -xf/path (from the first
    / ~ or . on). None when there is none."""
    if not a.startswith("-"):
        return None
    if "=" in a:
        v = a.split("=", 1)[1]
        return v if _is_path(v) else None
    i = next((k for k, ch in enumerate(a) if k > 1 and ch in "/~."), None)
    return a[i:] if i is not None and not a.startswith("--") else None


def _values(argv: list[str]) -> list[str]:
    """Every argument and every option-attached value: the strings that may name a file. A value with a
    colon also gives each piece: `file -m a:b` reads every file in the list (round 2)."""
    out = []
    for a in argv[1:]:
        vals = [a]
        if a.startswith("-") and "=" in a:
            vals.append(a.split("=", 1)[1])
        elif (v := _attached(a)) is not None:
            vals.append(v)
        out += vals + [p for v in vals if ":" in v for p in v.split(":") if p]
    return out


def _outputs(name: str, args: list[str]) -> list[str] | None:
    """The files an OUTPUT_OPT / OUTPUT_OPERAND tool writes; None when an -o has no value (fail closed)."""
    if name in OUTPUT_OPERAND:
        return [a for a in args if not a.startswith("-")][1:]
    out = []
    for i, a in enumerate(args):
        if a == "-out":  # tiffutil
            if i + 1 >= len(args):
                return None
            out.append(args[i + 1])
        elif a.startswith("--o"):  # --output, --output-file
            if "=" in a:
                out.append(a.split("=", 1)[1])
            elif i + 1 < len(args):
                out.append(args[i + 1])
            else:
                return None
        elif a.startswith("-") and not a.startswith("--") and "o" in a[1:]:
            tail = a[a.index("o", 1) + 1:]  # -o file, -ofile, -ro file
            if tail:
                out.append(tail)
            elif i + 1 < len(args):
                out.append(args[i + 1])
            else:
                return None
    return out


def _outside_scratch(targets: list[str], ctx: agent_policy.Ctx) -> bool:
    """Absolute paths in the agent's own scratch dir only, never a shared one like /tmp (spec flag c),
    and never a relative path: after a cd it lands elsewhere."""
    scratch = os.path.realpath(ctx.cwd) if ctx.cwd else None
    return scratch is None or not all(
        os.path.isabs(os.path.expanduser(t)) and _under(_resolve(t, ctx), scratch) for t in targets)


def _is_path(tok: str) -> bool:
    return "/" in tok or tok.startswith(("~", "."))


def _resolve(tok: str, ctx: agent_policy.Ctx, base: str | None = None) -> str:
    p = os.path.expanduser(tok.replace("~", ctx.home, 1) if tok.startswith("~") else tok)
    if not os.path.isabs(p):
        p = os.path.join(base if base is not None else (ctx.cwd or "."), p)
    return os.path.realpath(p)


def _places(tok: str, ctx: agent_policy.Ctx) -> list[str]:
    """Where a path may point: relative ones against the scratch dir AND home, since the shell's
    working directory is not ours to know once any cd ran (fail closed). A path on the macOS data
    volume (/System/Volumes/Data/Users/...) is also checked without that prefix: a firmlink makes it
    the same file, and realpath does not fold it (round 2)."""
    if os.path.isabs(os.path.expanduser(tok)) or tok.startswith("~"):
        places = [_resolve(tok, ctx)]
    else:
        places = [_resolve(tok, ctx), _resolve(tok, ctx, ctx.home)]
    return places + [os.path.realpath(p[len(DATA_VOLUME):] or "/") for p in places
                     if _under(p.casefold(), DATA_VOLUME)]


def _net(tok: str, ctx: agent_policy.Ctx) -> bool:
    """The token names a path under an automount point (NET_ROOTS), by text or through a symlink
    (/Volumes/Macintosh HD -> /). Relative ones are checked against the scratch dir and home, like _places."""
    if not _is_path(tok):
        return False
    p = os.path.expanduser(tok.replace("~", ctx.home, 1) if tok.startswith("~") else tok)
    bases = [p] if os.path.isabs(p) else [os.path.join(ctx.cwd or ".", p), os.path.join(ctx.home, p)]
    for b in bases:
        n = ("/" + os.path.abspath(b).lstrip("/")).casefold()  # abspath keeps a leading //
        # the walk gets the unfolded path: the kernel follows a link before a later `..` (link/../net)
        if any(_under(n, r) for r in NET_ROOTS) or _net_walk(b if os.path.isabs(b) else os.path.join(os.getcwd(), b)):
            return True
    return False


def _is_net(path: str) -> bool:
    return any(_under(path.casefold(), r) for r in NET_ROOTS)


def _net_walk(path: str) -> bool:
    """Resolve the path one component at a time, following symlinks by hand, and stop as soon as the
    text lands on or under a NET_ROOT: nothing on or below /net is ever stat'ed, so no host lookup
    (realpath on a prefix would follow a link into /net/<host> and look it up)."""
    todo = [c for c in reversed(path.split("/"))]
    cur, hops = "/", 0
    while todo:
        c = todo.pop()
        if c in ("", "."):
            continue
        if c == "..":
            cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, c)
        if _is_net(nxt):
            return True
        if not os.path.islink(nxt):
            cur = nxt
            continue
        hops += 1
        try:
            target = os.readlink(nxt)
        except OSError:
            return True  # fail closed
        if hops > 40:
            return True  # a link loop: fail closed
        todo.extend(reversed(target.split("/")))
        if target.startswith("/"):
            cur = "/"
    return False


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
    if any(_net(v, ctx) for v in [argv[0], *values]):  # before any realpath below
        return "network"
    if name in SUDO:
        return "sudo"
    if (name in RUNS_CODE or name.startswith(RUNS_CODE_PREFIXES)
            or (name == "find" and any(a in FIND_RUNS for a in args))):
        return "runs_code"
    guard_roots = [os.path.realpath(os.path.join(ctx.home, d)) for d in GUARD_HOME]
    guard_roots += [os.path.realpath(str(agent.SUPPORT_DIR)), os.path.realpath(str(SETTINGS_FILE.parent))]
    guard_roots += [os.path.realpath(d) for d in GUARD_ABS]
    if name in GUARD or any(_is_path(v) and any(_under(p, r) for p in _places(v, ctx) for r in guard_roots)
                            for v in values):
        return "guard"
    if name in NETWORK or name.startswith(NETWORK_PREFIXES):
        return "network"
    if name == "git":
        sub, rest = _git_sub(argv)
        if sub == "push":
            return "network"
        if sub not in GIT_AUTO or (sub == "stash" and rest and rest[0] not in STASH_AUTO):
            return "git"
    if name in DELETE:
        # plain arguments plus option-attached paths (tar --directory=/elsewhere, tar -C/elsewhere)
        targets = [a for a in args if not a.startswith("-")] + [
            v for a in args if (v := _attached(a)) is not None]
        if not targets or _outside_scratch(targets, ctx):
            return "delete"
    if name in OUTPUT_OPT or name in OUTPUT_OPERAND:
        outputs = _outputs(name, args)
        if outputs is None or (outputs and _outside_scratch(outputs, ctx)):
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
    return argvs is not None and all(_base(a) in READ_ONLY and not _file_reads_list(a) for a in argvs)


def _file_reads_list(argv: list[str]) -> bool:
    """`file` with -m/--magic-file (prints the lines of magic files it cannot parse), -C or -f (reads
    names from a file): not a plain look-up (round 2)."""
    return _base(argv) == "file" and any(
        a.startswith(("--magic-file", "--compile", "--files-from"))
        or (a.startswith("-") and not a.startswith("--") and any(c in a[1:] for c in "mCf"))
        for a in argv[1:])


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
