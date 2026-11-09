"""Deterministic overlay intents: transcript -> open an app/site, clear, ground, diff or answer.

First stop before any model call. "open X" either launches a real macOS app
(the name must match an entry from the app dirs — it never reaches `open` as
raw transcript text, and argv lists only, never a shell string) or a strictly
validated https URL. Everything needing the model (grounding, git diff, plain
questions) comes back as an Intent for the turn pipeline.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Intent:
    kind: str  # open_app | open_site | clear | ground | git_diff | answer
    arg: str = ""


APP_DIRS = ("/Applications", "/System/Applications", "/System/Applications/Utilities", "~/Applications")

# Host: ordinary DNS labels only. Path (after the first /): a fixed safe
# charset — nothing that could turn into a scheme or a second argument.
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PATH_RE = re.compile(r"^[/A-Za-z0-9._~%-]*$")
_WAKE_RE = re.compile(r"^(?:hey\s+)?jarvis[,:!\s]+", re.I)
_GROUND_LEADS = ("show me", "where is", "which button", "point to", "highlight")


def parse(text: str) -> Intent:
    """Transcript -> Intent; the cleaned transcript is the arg for the model kinds."""
    t = _WAKE_RE.sub("", text.strip(), count=1).strip().rstrip(".?!").strip()
    low = t.lower()
    if low in ("clear", "clear the screen"):
        return Intent("clear")
    # "the changes since the last merge", "show the diff" — before ground,
    # whose leads ("highlight ...") would otherwise swallow these.
    if "diff" in low or ("changes" in low and "merge" in low):
        return Intent("git_diff", t)
    if low.startswith(_GROUND_LEADS):
        return Intent("ground", t)
    if low.startswith("open "):
        arg = t[5:].strip()
        host = arg.lower().partition("/")[0]
        kind = "open_site" if _HOST_RE.match(host) else "open_app"
        return Intent(kind, arg)
    return Intent("answer", t)


def list_apps(dirs: tuple[str, ...] = APP_DIRS) -> dict[str, str]:
    """Dir list -> {lowercase name: real name} from *.app entries; missing dirs skipped."""
    apps: dict[str, str] = {}
    for d in dirs:
        if d.startswith("~"):
            d = str(Path.home()) + d[1:]
        path = Path(d)
        if not path.is_dir():
            continue
        for entry in path.glob("*.app"):
            apps.setdefault(entry.stem.lower(), entry.stem)
    return apps


def open_app(name: str, *, run=subprocess.run, apps: dict[str, str] | None = None) -> str | None:
    """Launch a real app by validated name; None = refused, nothing run.

    Only a matched value from the real app list is ever passed to `open`, so
    transcript noise (shell metacharacters, flags) can never become a command.
    """
    if apps is None:
        apps = list_apps()
    low = name.strip().lower()
    if not low:
        return None
    if low in apps:
        real = apps[low]
    else:
        matches = [v for k, v in apps.items() if k.startswith(low)]
        if len(matches) != 1:
            return None
        real = matches[0]
    run(["open", "-a", real], check=False)
    return real


def open_site(target: str, *, run=subprocess.run) -> str | None:
    """Open an https URL built from a validated host + safe path; None = refused."""
    target = target.strip()
    host, sep, path = target.partition("/")
    if not _HOST_RE.match(host.lower()):
        return None
    if sep and not _PATH_RE.match(path):
        return None
    url = "https://" + target
    run(["open", url], check=False)
    return url
