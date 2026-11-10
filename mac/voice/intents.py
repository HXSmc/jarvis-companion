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
    kind: str  # open_app | open_site | clear | clock | ground | git_diff | answer
    arg: str = ""


APP_DIRS = ("/Applications", "/System/Applications", "/System/Applications/Utilities", "~/Applications")

# Host: ordinary DNS labels only. Path (after the first /): a fixed safe
# charset — nothing that could turn into a scheme or a second argument.
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PATH_RE = re.compile(r"^[/A-Za-z0-9._~%-]*$")
_WAKE_RE = re.compile(r"^(?:hey\s+)?jarvis[,:!\s]+", re.I)
# STT often turns the wake phrase into other words: "Hey, John Pierce. What time is it".
_MISHEARD_WAKE_RE = re.compile(r"^(?:hey|hi|ok(?:ay)?)(?:[\s,]+\w+){1,3}[.!?]\s+", re.I)
_GROUND_LEADS = ("show me", "where is", "where's", "which button", "point to",
                 "point at", "highlight", "find the")
# UI things on screen; anything else after a lead is a question for the model.
UI_NOUNS = ("button", "menu", "icon", "tab", "field", "toggle", "switch", "checkbox",
            "link", "setting", "settings", "option", "toolbar", "sidebar", "dropdown",
            "slider", "box", "bar", "item", "key", "control")
# Word boundaries only: "difference"/"different"/"diffusion" are not diffs.
_DIFF_RE = re.compile(r"\bdiffs?\b|\bwhat changed\b")
_CHANGES_RE = re.compile(r"\bchang(e|es|ed)\b")
_MERGE_RE = re.compile(r"\bmerge[sd]?\b")
_GROUND_LEAD_RE = re.compile(r"\b(" + "|".join(_GROUND_LEADS) + r")\b")
_UI_NOUN_RE = re.compile(r"\b(" + "|".join(UI_NOUNS) + r")\b")
# "open the clock app" -> "clock"; "notes.app" is a bundle, not a domain.
_APP_NOISE_RE = re.compile(r"^the\s+|(?:\.app|\s+app)$", re.I)
# Whole-utterance clock questions only: "what time is it in Tokyo" still goes to the brain.
_TIME_RE = re.compile(r"^(?:what(?:'s| is) the time|what time is it|tell me the time)(?: now| right now| please)?$")
_DATE_RE = re.compile(r"^(?:what(?:'s| is) (?:the date|today's date|the day)|what day is (?:it|today)"
                      r"|what date is it|tell me the date)(?: today| now| please)?$")


def parse(text: str, apps: dict[str, str] | None = None) -> Intent:
    """Transcript -> Intent; the cleaned transcript is the arg for the model kinds.

    `apps` (None -> list_apps()) is only consulted to resolve "open <name>.app"
    to a real installed app before the domain check.
    """
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    t = _MISHEARD_WAKE_RE.sub("", t, count=1).strip().rstrip(".?!").strip()
    low = t.lower()
    if low in ("clear", "clear the screen"):
        return Intent("clear")
    if _TIME_RE.match(low):
        return Intent("clock", "time")
    if _DATE_RE.match(low):
        return Intent("clock", "date")
    # "the changes since the last merge", "show the diff" — before ground,
    # whose leads ("highlight ...") would otherwise swallow these.
    if _DIFF_RE.search(low) or (_CHANGES_RE.search(low) and _MERGE_RE.search(low)):
        return Intent("git_diff", t)
    # Ground only for a UI thing on screen: a lead followed by a UI noun,
    # else "where is Paris" would ask the screen instead of the model.
    lead = _GROUND_LEAD_RE.search(low)
    if lead and _UI_NOUN_RE.search(low, lead.end()):
        return Intent("ground", t)
    if low.startswith("open "):
        arg = t[5:].strip()
        name = _APP_NOISE_RE.sub("", arg).strip()
        if _match_app(name, apps if apps is not None else list_apps()):
            return Intent("open_app", name)
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


def _match_app(name: str, apps: dict[str, str]) -> str | None:
    """Real app name for a transcript name (exact, then unique prefix); None = no match."""
    low = name.strip().lower()
    if not low:
        return None
    if low in apps:
        return apps[low]
    matches = [v for k, v in apps.items() if k.startswith(low)]
    return matches[0] if len(matches) == 1 else None


def open_app(name: str, *, run=subprocess.run, apps: dict[str, str] | None = None) -> str | None:
    """Launch a real app by validated name; None = refused, nothing run.

    Only a matched value from the real app list is ever passed to `open`, so
    transcript noise (shell metacharacters, flags) can never become a command.
    """
    if apps is None:
        apps = list_apps()
    real = _match_app(name, apps)
    if real is None:
        return None
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
