"""Host policy for the agent tier: every tool call the agent CLI asks for is decided here.

Trust model: the agent reads untrusted text (files, commits, PRs, mail) by design, so the
policy, not the model, decides. Reads under $HOME are allowed minus a secrets deny list;
git and gh run only in read-only forms that we re-build from validated tokens (the model's
raw string never runs); anything with a side effect becomes Confirm (the owner's spoken
yes); everything else is denied. `decide` never raises.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mac.voice import gitdiff


@dataclass(frozen=True)
class Allow:
    updated_input: dict


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Confirm:
    summary: str
    updated_input: dict


def _toplevel(path: str) -> str | None:
    out = gitdiff._git(Path(path), "rev-parse", "--show-toplevel")
    return out.splitlines()[0].strip() if out else None


@dataclass
class Ctx:
    home: str = field(default_factory=lambda: str(Path.home()))
    vault: str | None = field(default_factory=lambda: os.environ.get("OVERLAY_VAULT") or None)
    cwd: str = ""
    read_only_mcp: frozenset = frozenset()
    read_untrusted_remote: bool = False  # this turn already read PR/issue/mail text
    trusted_repos: frozenset = frozenset()  # gh -R values used before that happened
    toplevel: Callable[[str], str | None] = _toplevel
    has_filters: Callable[[str], bool] = lambda d: gitdiff.has_filters(Path(d))


_DENY_HOME = (".ssh", ".gnupg", ".aws", ".config/gh", ".config/glm", ".netrc", ".git-credentials",
              ".npmrc", ".pypirc", ".docker", ".kube", ".password-store", "Library/Keychains",
              "Library/Cookies", "Library/Application Support/JarvisOverlay", ".claude.json")
_DENY_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_*", "*.p12", "*.keychain*")
_GREP_EXCLUDE = "!{.env,.env.*,*.pem,*.key,id_*,*.p12}"
_SECRET_SAMPLES = (".env", ".env.local", "x.pem", "x.key", "id_rsa", "id_ed25519", "x.p12")
_METACHARS = set(";|&<>()$`\n\r\\")
_GIT_READ = {"log", "show", "diff", "status", "blame", "rev-parse", "ls-files", "shortlog", "branch"}
_GIT_BAD_ARGS = ("--output", "-o", "--ext-diff", "--textconv", "--exec-path", "--git-dir", "--work-tree",
                 "-c", "--config-env", "--upload-pack", "--open-files-in-pager", "-O", "--no-index")
_BRANCH_WRITE = {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--move", "--copy", "--force",
                 "--set-upstream-to", "-u", "--unset-upstream", "--edit-description"}
_GH_READ = {"pr": {"list", "view", "diff", "checks", "status"}, "issue": {"list", "view", "status"},
            "repo": {"view", "list"}, "run": {"list", "view"}, "release": {"list", "view"}}
_GH_REMOTE_TEXT = {("pr", "view"), ("pr", "diff"), ("issue", "view"), ("run", "view"), ("release", "view")}
_GH_DENY = {"--web", "-w", "--hostname", "--watch"}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_API_PATH_RE = re.compile(r"^/?(repos|users|orgs|search|notifications|user)(/|$|\?)")
_GH_ENV = "env GH_PAGER=cat GH_PROMPT_DISABLED=1 NO_COLOR=1 "
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_SEND_WORDS = ("send", "reply", "forward")


def _fold(p: str) -> str:
    return p.casefold().rstrip("/") or "/"


def _under(path: str, root: str) -> bool:
    """Case-folded containment (macOS paths are case-insensitive; normcase does not fold there)."""
    p, r = _fold(path), _fold(root)
    return p == r or p.startswith(r + "/")


def _deny_roots(ctx: Ctx) -> list[str]:
    home = os.path.realpath(ctx.home)
    roots = [os.path.realpath(os.path.join(home, d)) for d in _DENY_HOME]
    if ctx.vault:
        roots.append(os.path.realpath(os.path.join(os.path.expanduser(ctx.vault), "Secrets")))
    return roots


def _secret_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name.casefold(), pat) for pat in _DENY_NAMES)


def _claude_credentials(path: str, home: str) -> bool:
    rel = _fold(path)[len(_fold(home)) + 1:].split("/") if _under(path, home) else []
    return bool(rel) and rel[0].startswith(".claude") and any(c.startswith(".credentials") for c in rel)


def _resolve(raw, ctx: Ctx) -> str:
    if raw is None or raw == "":
        return os.path.realpath(ctx.cwd or ".")
    if not isinstance(raw, str):
        raise TypeError("path must be a string")
    p = os.path.expanduser(raw)
    if not os.path.isabs(p):
        p = os.path.join(ctx.cwd or ".", p)
    return os.path.realpath(p)


def _path_problem(raw, resolved: str, ctx: Ctx, search: bool) -> str | None:
    """Why this path may not be read (None = fine). `search` = Grep/Glob: also refuse ancestors of secrets."""
    home = os.path.realpath(ctx.home)
    if not (_under(resolved, home) or (ctx.cwd and _under(resolved, os.path.realpath(ctx.cwd)))):
        return "only files in the owner's home folder can be read"
    names = [os.path.basename(resolved)] + ([os.path.basename(raw)] if isinstance(raw, str) else [])
    if any(_secret_name(n) for n in names if n) or _claude_credentials(resolved, home):
        return "that file holds secrets and is off limits"
    roots = _deny_roots(ctx)
    if any(_under(resolved, r) for r in roots):
        return "that folder holds secrets and is off limits"
    if search and any(_under(r, resolved) for r in roots):
        try:
            entries = sorted(e for e in os.listdir(resolved)
                             if not _secret_name(e)
                             and not any(_under(os.path.join(resolved, e), r) for r in roots))[:20]
        except OSError:
            entries = []
        return ("that folder contains secrets, which are off limits. Search one of these folders instead: "
                + ", ".join(entries))
    return None


def _files(tool: str, inp: dict, ctx: Ctx) -> Allow | Deny:
    key = "file_path" if tool == "Read" else "path"
    raw = inp.get(key)
    if tool == "Read" and not raw:
        return Deny("no file given")
    resolved = _resolve(raw, ctx)
    problem = _path_problem(raw, resolved, ctx, search=tool != "Read")
    if problem:
        return Deny(problem)
    out = {**inp, key: resolved}
    pattern_key = {"Glob": "pattern", "Grep": "glob"}.get(tool)
    pattern = inp.get(pattern_key) if pattern_key else None
    if pattern is not None:
        if not isinstance(pattern, str) or pattern.startswith(("/", "~")) or ".." in pattern:
            return Deny("patterns must stay inside the folder")
    if tool == "Grep" and os.path.isdir(resolved):
        if pattern is None:
            out["glob"] = _GREP_EXCLUDE
        elif any(fnmatch.fnmatch(s, pattern.removeprefix("**/")) for s in _SECRET_SAMPLES):
            return Deny("that search would read secret files; narrow the glob, for example *.py")
    return Allow(out)


def _clip(text: str, limit: int = 120) -> str:
    text = " ".join(text.replace("<", "").replace(">", "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _git_dir(raw: str, ctx: Ctx) -> tuple[str, str] | Deny:
    """(dir, toplevel) for `git -C <raw>`, or why git may not run there."""
    d = os.path.realpath(os.path.expanduser(raw))
    if not _under(d, os.path.realpath(ctx.home)):
        return Deny("git only runs in repositories in the owner's home folder")
    top = ctx.toplevel(d)
    if not top:
        return Deny("that folder is not a git repository")
    top = os.path.realpath(top)
    vault = os.path.realpath(os.path.expanduser(ctx.vault)) if ctx.vault else None
    if vault and (_under(top, vault) or _under(vault, top)):
        return Deny("git does not run on the notes vault")
    if any(_under(r, top) for r in _deny_roots(ctx)):
        return Deny("that repository contains secret folders")
    if ctx.has_filters(top):
        return Deny("that repository defines its own git filters")
    return d, top


def _git(tokens: list[str], ctx: Ctx) -> tuple[str, list[str], str, bool] | Deny:
    """Validated `git -C dir sub args` -> (dir, defused argv, repo name, read_only)."""
    if len(tokens) < 4 or tokens[0] != "git" or tokens[1] != "-C" or tokens[3].startswith("-"):
        return Deny("use the form: git -C <repo> <subcommand> ...")
    where = _git_dir(tokens[2], ctx)
    if isinstance(where, Deny):
        return where
    d, top = where
    sub, args = tokens[3], tokens[4:]
    for a in args:
        if any(a == b or a.startswith(b + "=") for b in _GIT_BAD_ARGS):
            return Deny(f"git option {a.split('=')[0]} is not allowed")
        if _secret_name(re.split(r"[:/]", a)[-1]):
            return Deny("that file holds secrets and is off limits")
    read_only = sub in _GIT_READ and (sub != "branch" or ("--list" in args and not _BRANCH_WRITE & set(args)))
    extra = []
    if read_only and sub in ("diff", "log", "show"):
        extra.append("--no-ext-diff")
    if read_only and sub in ("diff", "log", "show", "blame"):
        extra.append("--no-textconv")
    argv = gitdiff._GIT_PREFIX + ["-C", d, sub, *extra, *args]
    return d, argv, os.path.basename(top), read_only


def _bash(inp: dict, ctx: Ctx) -> Allow | Deny | Confirm:
    cmd = inp.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return Deny("no command given")
    parts = [cmd]
    if " && " in cmd:  # only git chains, and only as one confirmed step
        parts = cmd.split(" && ")
        if len(parts) > 4:
            return Deny("too many steps in one command")
    if any(_METACHARS & set(p) for p in parts):
        return Deny("one plain command per call: no pipes, redirects, substitutions or ;")
    try:
        token_lists = [shlex.split(p) for p in parts]
    except ValueError:
        return Deny("could not parse that command")
    if not all(token_lists):
        return Deny("no command given")
    if len(token_lists) > 1:
        built = [_git(t, ctx) for t in token_lists]
        for b in built:
            if isinstance(b, Deny):
                return b
        if len({b[0] for b in built}) != 1:
            return Deny("a chain must stay in one repository")
        summary = (f"run git {', '.join(shlex.join(t[3:]) for t in token_lists)} in {built[0][2]}")
        return _confirm_bash(summary, inp, " && ".join(shlex.join(b[1]) for b in built))
    tokens = token_lists[0]
    if tokens[0] == "git":
        b = _git(tokens, ctx)
        if isinstance(b, Deny):
            return b
        d, argv, repo, read_only = b
        if read_only:
            return Allow({**inp, "command": shlex.join(argv)})
        return _confirm_bash(f"run git {shlex.join(tokens[3:])} in {repo}", inp, shlex.join(argv))
    if tokens[0] == "gh":
        return _gh(tokens, inp, ctx)
    return _confirm_bash(f"run {cmd}", inp, cmd)


def _confirm_bash(summary: str, inp: dict, command: str) -> Confirm | Deny:
    clean = _clip(summary, 10_000)
    if len(clean) > 120:  # the owner must hear exactly what runs
        return Deny("too long to confirm out loud; split it into smaller steps")
    return Confirm(clean, {**inp, "command": command})


def _gh_repo(args: list[str]) -> str | None | Deny:
    for i, a in enumerate(args):
        value = None
        if a in ("-R", "--repo"):
            value = args[i + 1] if i + 1 < len(args) else ""
        elif a.startswith("--repo="):
            value = a.split("=", 1)[1]
        if value is not None:
            return value if _REPO_RE.match(value) else Deny("not a valid owner/repo")
    return None


def _gh(tokens: list[str], inp: dict, ctx: Ctx) -> Allow | Deny | Confirm:
    args = tokens[1:]
    if any(a in _GH_DENY or a.split("=")[0] in _GH_DENY for a in args):
        return Deny("gh may not open a browser, watch, or talk to another host")
    repo = _gh_repo(args)
    if isinstance(repo, Deny):
        return repo
    command = _GH_ENV + shlex.join(tokens)
    side = _confirm_bash(f"run {shlex.join(tokens)}", inp, command)
    if not args:
        return Deny("no gh command given")
    if args[0] == "api":
        rest, positional, i, post = args[1:], [], 0, False
        while i < len(rest):
            a = rest[i]
            if a in ("--jq", "-q"):
                i += 2
                continue
            if a in ("-X", "--method"):
                post = post or i + 1 >= len(rest) or rest[i + 1].upper() != "GET"
                i += 2
                continue
            if a.startswith("--method="):
                post = post or a.split("=", 1)[1].upper() != "GET"
            elif a.startswith("--jq=") or a in ("--paginate", "--include", "-i", "--silent"):
                pass
            elif a in ("-f", "-F", "--field", "--raw-field", "-H", "--header", "--input"):
                post = True  # fields or a body make it a write, not a plain GET
                i += 2
                continue
            elif a.startswith("-"):
                post = True
            else:
                positional.append(a)
            i += 1
        if len(positional) != 1 or not _API_PATH_RE.match(positional[0]):
            return Deny("gh api only reads repos, users, orgs, search or notifications")
        if post or ctx.read_untrusted_remote:
            return side
        return Allow({**inp, "command": command})
    group, verb = args[0], (args[1] if len(args) > 1 else "")
    if verb not in _GH_READ.get(group, ()):
        return side
    if ctx.read_untrusted_remote and repo not in ctx.trusted_repos:
        return side  # after reading untrusted text, a new repo name could carry data out
    return Allow({**inp, "command": command})


def _mcp(tool: str, inp: dict, ctx: Ctx) -> Allow | Confirm:
    if tool in ctx.read_only_mcp:
        return Allow(inp)
    _, server, name = (tool.split("__", 2) + ["", ""])[:3]
    if any(w in name.casefold() for w in _SEND_WORDS):
        to = next((inp[k] for k in ("to", "recipient", "recipients") if k in inp), "someone")
        to = ", ".join(map(str, to)) if isinstance(to, list) else str(to)
        summary = f"send an email to {to} with subject {inp.get('subject', '(none)')}"
    else:
        fields = [f"{k}={v}" for k, v in inp.items() if isinstance(v, str) and len(v) <= 40][:3]
        summary = f"use {name} on {server}" + (f" ({', '.join(fields)})" if fields else "")
    return Confirm(_clip(summary), inp)


def decide(tool_name: str, tool_input: dict, ctx: Ctx) -> Allow | Deny | Confirm:
    """The one gate every agent tool call passes. Never raises; unknown = Deny."""
    try:
        if not isinstance(tool_input, dict):
            return Deny("bad tool input")
        if tool_name in ("Read", "Grep", "Glob"):
            return _files(tool_name, tool_input, ctx)
        if tool_name == "Bash":
            return _bash(tool_input, ctx)
        if tool_name in _WRITE_TOOLS:
            key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
            raw = tool_input.get(key)
            if not isinstance(raw, str) or not raw:
                return Deny("no file given")
            resolved = _resolve(raw, ctx)
            problem = _path_problem(raw, resolved, ctx, search=False)
            if problem:
                return Deny(problem)
            parent = os.path.basename(os.path.dirname(resolved))
            return Confirm(_clip(f"write to {os.path.basename(resolved)} in {parent}"),
                           {**tool_input, key: resolved})
        if tool_name.startswith("mcp__"):
            return _mcp(tool_name, tool_input, ctx)
        return Deny(f"the {tool_name or 'unnamed'} tool is not available")
    except Exception:
        return Deny("policy error")


def _tokens(tool_input) -> list[str]:
    try:
        return shlex.split(str(tool_input.get("command") or "")) if isinstance(tool_input, dict) else []
    except ValueError:
        return []


def reads_remote_text(tool_name: str, tool_input: dict) -> bool:
    """True when the call returns text other people wrote remotely (PRs, issues, runs, mail)."""
    if tool_name.startswith("mcp__"):
        return True
    t = _tokens(tool_input) if tool_name == "Bash" else []
    if len(t) >= 2 and t[0] == "gh":
        return t[1] == "api" or (len(t) >= 3 and (t[1], t[2]) in _GH_REMOTE_TEXT)
    return False


def kind_of(tool_name: str, tool_input: dict, ctx: Ctx) -> str:
    """Progress-line family: git | gh | notes | mail | calendar | file | other."""
    if tool_name.startswith("mcp__"):
        server = tool_name.split("__")[1].casefold() if tool_name.count("__") >= 2 else ""
        return "mail" if "mail" in server else "calendar" if "calendar" in server else "other"
    if tool_name == "Bash":
        t = _tokens(tool_input)
        return t[0] if t and t[0] in ("git", "gh") else "other"
    if tool_name in ("Read", "Grep", "Glob"):
        raw = tool_input.get("file_path" if tool_name == "Read" else "path") if isinstance(tool_input, dict) else None
        try:
            resolved = _resolve(raw, ctx)
        except TypeError:
            return "file"
        vault = os.path.realpath(os.path.expanduser(ctx.vault)) if ctx.vault else None
        return "notes" if vault and _under(resolved, vault) else "file"
    return "other"


def log_label(tool_name: str, tool_input: dict) -> str:
    """What the logs and the debug page may show: the tool and git/gh subcommand, never arguments."""
    if tool_name != "Bash":
        return tool_name if re.fullmatch(r"[A-Za-z0-9_]{1,80}", tool_name or "") else "unknown tool"
    t = _tokens(tool_input)
    if t[:2] == ["git", "-C"] and len(t) >= 4 and re.fullmatch(r"[a-z-]{1,20}", t[3]):
        return f"Bash git {t[3]}"
    if t and t[0] == "gh" and len(t) >= 2 and re.fullmatch(r"[a-z-]{1,20}", t[1]):
        return f"Bash gh {t[1]}"
    return "Bash other"
