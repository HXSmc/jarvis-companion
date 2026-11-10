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
    # The session already read text it did not write (files, commits, PRs, mail). Set by the gateway
    # after any allowed call, kept for the session's life: from then on a new repo name could carry data out.
    read_untrusted_remote: bool = False
    trusted_repos: frozenset = frozenset()  # owner/repo values used before that happened (lower case)
    toplevel: Callable[[str], str | None] = _toplevel
    has_filters: Callable[[str], bool] = lambda d: gitdiff.has_filters(Path(d))


_DENY_HOME = (".ssh", ".gnupg", ".aws", ".azure", ".oci", ".config/gh", ".config/glm", ".config/gcloud",
              ".config/op", ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".docker", ".kube",
              ".password-store", ".terraform.d", "Library/Keychains", "Library/Cookies", "Library/Mail",
              "Library/Messages", "Library/Safari", "Library/Containers/com.apple.Safari",
              "Library/Application Support/Google", "Library/Application Support/Firefox",
              "Library/Application Support/BraveSoftware", "Library/Application Support/Microsoft Edge",
              "Library/Application Support/Arc", "Library/Application Support/com.apple.TCC",
              "Library/Application Support/JarvisOverlay", ".claude.json")
_DENY_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_*", "*.p12", "*.pfx", "*.keychain*", "*credential*",
               "*token*.json", "*oauth*.json", "*.token", ".*_history", ".*history", ".pgpass", ".htpasswd",
               "*.kdbx", "*.keystore", "*.jks", ".npmrc", ".pypirc", ".netrc", ".envrc", "secrets.*",
               "*.tfvars", "*.tfstate", "*.tfstate.*", "auth.json")
_SECRET_DIR = "secrets"  # any folder of this name (any case) is closed, OVERLAY_VAULT set or not
_WALK_DIRS = 4000  # a search or git root bigger than this is refused: we could not rule out a secrets folder
_WALK_DEPTH = 8
# Appended after any model glob: the CLI's Grep runs rg --hidden and splits `glob` on whitespace into
# --glob flags, and the later flag wins, so these stay excluded whatever the model asked for.
_GREP_EXCLUDE = "!{" + ",".join(_DENY_NAMES) + "}"
_SECRET_SAMPLES = (".env", ".env.local", "x.pem", "x.key", "id_rsa", "id_ed25519", "x.p12",
                   "credentials.json", "token.json", ".npmrc", ".pypirc", ".netrc", ".envrc", "auth.json",
                   "x.tfvars", "terraform.tfstate")
_GLOB_RE = re.compile(r"^[A-Za-z0-9_*./-]+$")  # no { } [ ] ! ? : ripgrep would read those its own way
_METACHARS = set(";|&<>()$`\n\r\\")
_GIT_READ = {"log", "show", "diff", "status", "blame", "rev-parse", "ls-files", "shortlog", "branch"}
# Long options that write, run a program, change the repo, or read a file outside history. An argument
# that is a prefix of one is refused too, since git accepts unambiguous abbreviations.
_GIT_BAD_LONG = ("--output", "--ext-diff", "--textconv", "--exec-path", "--git-dir", "--work-tree",
                 "--config-env", "--upload-pack", "--open-files-in-pager", "--no-index", "--contents",
                 "--ignore-revs-file", "--exclude-from", "--exclude-per-directory", "--pathspec-from-file",
                 "--orderfile", "--stdin", "--output-directory")
_GIT_BAD_SHORT = set("ocOX")  # -o/-c as the first letter; -O<file>/-X<file> anywhere in a cluster
_BRANCH_WRITE = {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--move", "--copy", "--force",
                 "--set-upstream-to", "-u", "--unset-upstream", "--edit-description"}
_GH_READ = {"pr": {"list", "view", "diff", "checks", "status"}, "issue": {"list", "view", "status"},
            "repo": {"view", "list"}, "run": {"list", "view"}, "release": {"list", "view"}}
_GH_DENY = {"--web", "-w", "--hostname", "--watch"}
_REPO_RE = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")  # GitHub owners have no dots: no HOST/ form
_API_PATH_RE = re.compile(r"^/?(repos|users|orgs|search|notifications|user)(/|$|\?)")
_GH_ENV = ("env -u GH_HOST -u GH_REPO -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN "
           "GH_PAGER=cat GH_PROMPT_DISABLED=1 NO_COLOR=1 ")
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_SEND_WORDS = ("send", "reply", "forward")
_ADDR_KEYS = {"to", "cc", "bcc", "recipient", "recipients", "attendees", "guests", "participants",
              "emails", "invitees", "reply_to", "replyto"}
_BODY_KEYS = {"body", "text", "content", "message", "description", "html", "htmlbody", "notes"}
# Writes the owner could not judge from a short spoken path: they run code later or change agent setup.
_WRITE_DENY_HOME = ("Library/LaunchAgents", "Library/LaunchDaemons", ".config/autostart", ".gitconfig",
                    ".config/git", ".ssh")
_WRITE_DENY_NAMES = (".zshrc", ".zshenv", ".zprofile", ".zlogin", ".zlogout", ".bashrc", ".bash_profile",
                     ".bash_login", ".profile", ".inputrc", ".gitconfig")


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


def _home_parts(path: str, home: str) -> list[str]:
    """Case-folded path components below home ([] when not under it)."""
    return [c for c in _fold(path)[len(_fold(home)) + 1:].split("/") if c] if _under(path, home) else []


def _closed_dir(path: str, home: str) -> bool:
    """A `secrets` folder anywhere, or the agent config / transcripts under ~/.claude*."""
    parts = _home_parts(path, home)
    return _SECRET_DIR in parts or bool(parts and parts[0].startswith(".claude"))


def _holds_closed(root: str, ctx: Ctx, links: bool = True) -> bool:
    """True when root contains a secrets folder or a deny root (breadth-first; symlinks checked when
    `links`, never followed), or is too big to tell: fail closed. git stores a symlink as its target
    text and never reads through it, so git roots pass links=False; searches might follow them."""
    home = os.path.realpath(ctx.home)
    roots = _deny_roots(ctx)
    level, seen = [root], 0
    for _ in range(_WALK_DEPTH):
        nxt = []
        for d in level:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if e.name.casefold() == _SECRET_DIR:
                    return True
                if e.is_symlink():
                    if not links:
                        continue
                    target = os.path.realpath(e.path)
                    if _closed_dir(target, home) or any(_under(target, r) or _under(r, target) for r in roots):
                        return True
                elif e.is_dir() and e.name not in (".git", "node_modules"):
                    seen += 1
                    if seen > _WALK_DIRS:
                        return True
                    nxt.append(e.path)
        level = nxt
    return False


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
    if any(_secret_name(n) for n in names if n):
        return "that file holds secrets and is off limits"
    roots = _deny_roots(ctx)
    if _closed_dir(resolved, home) or any(_under(resolved, r) for r in roots):
        return "that folder holds secrets and is off limits"
    if search and os.path.isdir(resolved) and (any(_under(r, resolved) for r in roots)
                                               or _holds_closed(resolved, ctx)):
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
        elif (not _GLOB_RE.match(pattern)
              or any(fnmatch.fnmatch(s, pattern.rsplit("/", 1)[-1]) for s in _SECRET_SAMPLES)):
            return Deny("that search would read secret files; use a plain glob, for example *.py")
        else:
            out["glob"] = f"{pattern} {_GREP_EXCLUDE}"  # *.production would otherwise reach .env.production
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
    if any(_under(r, top) for r in _deny_roots(ctx)) or _closed_dir(top, ctx.home) or _holds_closed(top, ctx, links=False):
        return Deny("that repository contains secret folders, or is too big to check")
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
        problem = _git_arg_problem(a, sub, d, top, ctx)
        if problem:
            return Deny(problem)
    read_only = sub in _GIT_READ and (sub != "branch" or ("--list" in args and not _BRANCH_WRITE & set(args)))
    # `git remote` reads only as a bare list or get-url (add/set-url/remove write; show/update hit the network)
    read_only = read_only or (sub == "remote" and (args in ([], ["-v"], ["--verbose"])
                                                   or (len(args) == 2 and args[0] == "get-url")))
    extra = []
    if read_only and sub in ("diff", "log", "show"):
        extra.append("--no-ext-diff")
    if read_only and sub in ("diff", "log", "show", "blame"):
        extra.append("--no-textconv")
    argv = gitdiff._GIT_PREFIX + ["-C", d, sub, *extra, *args]
    return d, argv, os.path.basename(top), read_only


def _git_arg_problem(a: str, sub: str, d: str, top: str, ctx: Ctx) -> str | None:
    """Why one git argument is refused: a banned option, or a path that leaves the repo or holds secrets."""
    values = [a]
    if a.startswith("--"):
        name = a.split("=", 1)[0]
        if len(name) > 2 and any(b.startswith(name) for b in _GIT_BAD_LONG):
            return f"git option {name} is not allowed"
        if "=" in a:
            values.append(a.split("=", 1)[1])
    elif a.startswith("-") and len(a) > 1:
        if a[1] in _GIT_BAD_SHORT or "O" in a or "X" in a or (sub == "blame" and "S" in a):
            return f"git option {a[:2]} is not allowed"
        values.append(a[2:])
    parts = [c.casefold() for c in re.split(r"[:/]", a)]
    if _secret_name(parts[-1]) or _SECRET_DIR in parts:
        return "that file holds secrets and is off limits"
    if set("*?[") & set(a) and any(fnmatch.fnmatch(s, parts[-1]) for s in _SECRET_SAMPLES):
        return "that pathspec would match secret files"  # git log -p -- '*.env'

    for v in values:
        if not v or not (v.startswith(("/", "~")) or ".." in v.split("/") or os.path.lexists(os.path.join(d, v))):
            continue
        resolved = os.path.realpath(os.path.join(d, os.path.expanduser(v)))
        if not _under(resolved, top):
            return "git may only read files inside that repository"
        if _path_problem(v, resolved, ctx, search=False):
            return "that file holds secrets and is off limits"
    return None


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
    exact = shlex.join(tokens)  # quoted, so the shell expands no glob, brace or ~: it runs what was spoken
    return _confirm_bash(f"run {exact}", inp, exact)


def _confirm_bash(summary: str, inp: dict, command: str) -> Confirm | Deny:
    clean = _clip(summary, 10_000)
    if len(clean) > 120:  # the owner must hear exactly what runs
        return Deny("too long to confirm out loud; split it into smaller steps")
    return Confirm(clean, {**inp, "command": command})


def _gh_repo(args: list[str]) -> str | None | Deny:
    """The one -R/--repo value (gh uses the LAST one, so more than one is refused)."""
    values = []
    for i, a in enumerate(args):
        if a in ("-R", "--repo"):
            values.append(args[i + 1] if i + 1 < len(args) else "")
        elif a.startswith("--repo="):
            values.append(a.split("=", 1)[1])
        elif a.startswith("-R"):
            values.append(a[2:])
    if len(values) > 1:
        return Deny("give one repository only")
    if not values:
        return None
    return values[0] if _REPO_RE.match(values[0]) else Deny("not a valid owner/repo on github.com")


def gh_repo(command) -> str | None:
    """The valid -R repo of a gh command (lower case), else None. Used for the trusted-repos list."""
    t = _tokens({"command": command})
    repo = _gh_repo(t[1:]) if t[:1] == ["gh"] else None
    return repo.casefold() if isinstance(repo, str) else None


def _gh_host_problem(args: list[str]) -> str | None:
    """gh reaches only github.com: no URL, and no HOST/OWNER/REPO or dotted owner in any argument."""
    for a in args:
        if "://" in a:
            return "gh only talks to github.com: no URLs"
        if args[0] == "api":
            continue  # the api path is checked against its own allowlist
        v = a[2:] if a.startswith("-R") and len(a) > 2 else a.split("=", 1)[-1]
        if v.count("/") > 1 or ("/" in v and "." in v.split("/", 1)[0]):
            return "gh only talks to github.com: use owner/repo"
    return None


def _gh(tokens: list[str], inp: dict, ctx: Ctx) -> Allow | Deny | Confirm:
    args = tokens[1:]
    if any(a in _GH_DENY or a.split("=")[0] in _GH_DENY for a in args):
        return Deny("gh may not open a browser, watch, or talk to another host")
    if not args:
        return Deny("no gh command given")
    host = _gh_host_problem(args)
    if host:
        return Deny(host)
    repo = _gh_repo(args)
    if isinstance(repo, Deny):
        return repo
    command = _GH_ENV + shlex.join(tokens)
    side = _confirm_bash(f"run {shlex.join(tokens)}", inp, command)
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
        m = re.match(r"^/?repos/([^/?]+/[^/?]+)(?:[/?]|$)", positional[0])
        trusted = bool(m) and m.group(1).casefold() in ctx.trusted_repos
        if post or (ctx.read_untrusted_remote and not trusted):
            return side
        return Allow({**inp, "command": command})
    group, verb = args[0], (args[1] if len(args) > 1 else "")
    if verb not in _GH_READ.get(group, ()):
        return side
    if ctx.read_untrusted_remote and (repo or "").casefold() not in ctx.trusted_repos:
        return side  # after reading untrusted text, a new repo name could carry data out
    return Allow({**inp, "command": command})


def _mcp(tool: str, inp: dict, ctx: Ctx) -> Allow | Confirm | Deny:
    """Read-only list = Allow. Anything else = Confirm whose summary shows every recipient and every
    short field; a field the owner could not hear (nested, long, too much) means Deny."""
    if tool in ctx.read_only_mcp:
        return Allow(inp)
    _, server, name = (tool.split("__", 2) + ["", ""])[:3]
    addrs, fields = [], []
    for k, v in inp.items():
        key = str(k).casefold()
        if key in _ADDR_KEYS:
            vals = [v] if isinstance(v, str) else v
            if not isinstance(vals, list) or not all(isinstance(x, str) for x in vals):
                return Deny("I can't read those recipients out; ask for it in a simpler form")
            if vals:
                addrs.append(f"{k} {', '.join(vals)}")
        elif key in _BODY_KEYS and isinstance(v, str):
            continue  # the text itself is not read out; who gets it is
        elif isinstance(v, (bool, int, float)) or v is None or (isinstance(v, str) and len(v) <= 60):
            if key != "subject":
                fields.append(f"{k}={v}")
        else:
            return Deny("that request has fields I can't read out; ask for it in a simpler form")
    if any(w in name.casefold() for w in _SEND_WORDS):
        summary = f"send an email {'; '.join(addrs) or 'to nobody'} with subject {inp.get('subject', '(none)')}"
    else:
        summary = f"use {name} on {server}" + (f" for {'; '.join(addrs)}" if addrs else "")
        if "subject" in inp:
            fields.insert(0, f"subject={inp['subject']}")
    if fields:
        summary += f" ({', '.join(fields)})"
    clean = _clip(summary, 10_000)
    if len(clean) > 120:  # never clip: a hidden recipient or field is how an injection would hide
        return Deny("too much to confirm out loud; split it into smaller steps")
    return Confirm(clean, inp)


def _write(tool_name: str, tool_input: dict, ctx: Ctx) -> Confirm | Deny:
    key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    raw = tool_input.get(key)
    if not isinstance(raw, str) or not raw:
        return Deny("no file given")
    resolved = _resolve(raw, ctx)
    problem = _path_problem(raw, resolved, ctx, search=False)
    if problem:
        return Deny(problem)
    home = os.path.realpath(ctx.home)
    parts = _home_parts(resolved, home)
    if (".git" in parts or os.path.basename(resolved).casefold() in _WRITE_DENY_NAMES
            or any(_under(resolved, os.path.join(home, d)) for d in _WRITE_DENY_HOME)):
        return Deny("that is a startup, git or settings file; I won't change it")
    where = "~/" + os.path.relpath(resolved, home) if _under(resolved, home) else resolved
    summary = _clip(f"write to {where}", 10_000)
    if len(summary) > 120:
        return Deny("that path is too long to confirm out loud")
    return Confirm(summary, {**tool_input, key: resolved})


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
            return _write(tool_name, tool_input, ctx)
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
