"""Deterministic overlay intents: transcript -> open an app/site, clear, ground, diff,
owner-data agent work, or answer.

First stop before any model call. "open X" either launches a real macOS app
(the name must match an entry from the app dirs — it never reaches `open` as
raw transcript text, and argv lists only, never a shell string) or a strictly
validated https URL. Everything needing the model (grounding, git diff, agent
work, plain questions) comes back as an Intent for the turn pipeline.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Intent:
    kind: str  # open_app | open_site | clear | clock | ground | look | git_diff | agent | answer
    arg: str = ""
    sub: str = ""  # agent only: mail | calendar | notes | gh | git | code


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
# "highlight the last debugging session": a visual verb about something on screen but no UI
# noun -> "look" (ground against windows/text first, else the brain with screen context).
_LOOK_STRONG_RE = re.compile(r"\b(highlight|circle|underline|point (?:to|at|out))\b")
_LOOK_LEAD_RE = re.compile(r"\b(show me|where is|where's|where are|find|which)\b")
# Weak leads only count for "the last <...>" or a definite screen noun ("the debug page"):
# "the latest news", "this or that", "a good page on X" are questions for the brain.
_LOOK_REF_RE = re.compile(r"\b(?:the|my) (?:last|newest|previous)\b"
                          r"|\b(?:the|this|that|these|those|my)\s+(?:[\w-]+\s+){0,2}?"
                          r"(?:window|page|tab|screen|session|message|error|turn|line|section"
                          r"|panel|dialog|notification|card|button|field|link|menu|icon)s?\b")
# The utterance stops mid-thought: filler, article, possessive or conjunction at the end, or a
# trailing comma ("Can you show me the last, uh"). The user is still talking: wait for the rest.
FILLERS = ("uh", "um", "er", "erm", "uhm", "huh", "hm", "hmm", "mm", "ah")  # STT wrote "uh" as "huh"
_TRAILING = FILLERS + ("the", "a", "an", "my", "your", "our", "their", "his", "her", "and",
                       "or", "but", "to", "of", "last", "next", "plus", "minus")
_INCOMPLETE_RE = re.compile(r"(?:,|\b(?:" + "|".join(_TRAILING) + r"))\W*$", re.I)
_FILLER_TAIL_RE = re.compile(r"(?:[\s,]+(?:" + "|".join(FILLERS) + r"))+[\s,.?!]*$", re.I)
_DATE_RE = re.compile(r"^(?:what(?:'s| is) (?:the date|today's date|the day)|what day is (?:it|today)"
                      r"|what date is it|tell me the date)(?: today| now| please)?$")
# Owner-data agent work, first match in priority order picks the sub.
_AGENT_SUBS = (
    ("mail", re.compile(r"\b(?:inbox|unread|my e-?mails?|(?:any|new|latest|recent) (?:new )?e-?mails?"
                        r"|e-?mails? from|my mail|any mail|new mail"
                        r"|(?:read|check|search|find) (?:my |the )?(?:latest |last |recent |new )?e-?mails?)\b")),
    ("calendar", re.compile(r"\b(?:calendar|my meetings?|next meeting|meetings? (?:today|tomorrow|this week)"
                            r"|my schedule|agenda)\b")),
    ("notes", re.compile(r"\b(?:obsidian|vault|my notes|in my notes|note about)\b")),
    ("gh", re.compile(r"\b(?:prs?|pull requests?|github|(?:the|my) ci|ci (?:runs?|status|checks?|builds?)"
                      r"|workflow runs?|actions runs?|open issues|github issues|issues? (?:on|in|for)"
                      r"|issue #?\d+|my repos|repos? on github|releases? (?:of|on|for|in)|my releases)\b")),
    ("git", re.compile(r"\b(?:commits?|git history|commit history|file history"
                       r"|history of (?:this|the|that) (?:file|code|repo|function|project)|git log|commit log"
                       r"|blame|who changed|who wrote|what changed in|when did .* change|push my changes"
                       r"|my branch(?:es)?)\b")),
    ("code", re.compile(r"\b(?:the|this|that) (?:code|file|function|class|method) on (?:my|the) screen\b"
                        r"|\bthis (?:file|function|class|method)\b|\bcode (?:i have |i've got )?on (?:my|the) screen\b")),
)
# Generic questions ("what is a pull request") are for the brain even when they name
# agent-owned words; a possessive or pointer ("my calendar") makes it owner data.
_AGENT_LEADS = ("what is ", "what's ", "what are ", "explain ", "define ",
                "how does ", "how do ", "tell me about ")
_AGENT_GUARD_RE = re.compile(r"\b(?:my|our|this|these|that|open|last|latest|recent|on my screen|the repo)\b")
# Whole-utterance replies to the agent's confirmation question (or a cancel).
# Matched on the normalised whole utterance (lower case, commas gone): STT writes "Yes, please." and
# "Yeah, do it". Politeness may trail; any other words make it a new request, never a yes.
_POLITE = r"(?: (?:please|sir|jarvis|thanks|thank you))*"
_CONFIRM_RE = re.compile(r"(?:(?:yes|yeah|yep)(?: (?:please|sir))?(?: (?:do it|go ahead))?|do it|go ahead"
                         r"|confirm(?:ed)?)" + _POLITE)
_CANCEL_RE = re.compile(r"(?:stop(?: it)?|cancel(?: it| that)?|never ?mind|forget it|that's enough|abort)"
                        + _POLITE)
_NO_RE = re.compile(r"(?:(?:no|nope)(?: (?:don't|do not|thanks|thank you|stop|cancel(?: it)?|leave it))?"
                    r"|don't(?: do it)?|do not(?: do it)?)" + _POLITE)

# Overlay hide/show/aside/restore/move and the widened clear: whole-utterance
# phrases with an optional politeness wrapper ("can you hide it for now, please").
# Hide is checked before clear: "get rid of it" is temporary, "get rid of the cards" final.
_WRAP_LEAD = r"(?:(?:can|could|would) you |please |jarvis )*"
_WRAP_TRAIL = r"(?:,? (?:please|for me|for now|now|jarvis))*"
_OBJECT = r"(?:it|them|that|this|everything|all of it|all|the overlay|the cards?)"
_HIDE_RE = re.compile(_WRAP_LEAD + r"(?:hide(?: " + _OBJECT + r")?|get rid of it|put (?:it|them|everything) away"
                      r"|get (?:it|that|this|them) off my screen|go away"
                      r"|make (?:it|that|them|everything) (?:disappear|go away))" + _WRAP_TRAIL)
_SHOW_RE = re.compile(_WRAP_LEAD + r"(?:show (?:it|them|everything|the overlay|the cards?)(?: again)?"
                      r"|bring (?:it|them|everything) back|unhide(?: " + _OBJECT + r")?|come back)" + _WRAP_TRAIL)
_CLEAR_RE = re.compile(_WRAP_LEAD + r"(?:clear(?: it| them| all| everything| the (?:screen|overlay|cards?))?"
                       r"|dismiss(?: it| them| that| all| everything)?"
                       r"|close (?:that|everything)|remove the cards|get rid of the cards)" + _WRAP_TRAIL)
_ASIDE_RE = re.compile(_WRAP_LEAD + r"(?:move (?:it|them) (?:out of the way|aside)"
                       r"|park (?:it|the cards))" + _WRAP_TRAIL)
_RESTORE_RE = re.compile(_WRAP_LEAD + r"put (?:it|them|the cards) back" + _WRAP_TRAIL)
# Direction last ("move it to the top left", "move to the left side"); "top"->up, "bottom"->down
# at the match site. No "the screen" object: "move the screen left" is not about the cards.
_MOVE_RE = re.compile(_WRAP_LEAD + r"(?:move|shift)(?: " + _OBJECT + r")?"
                      + r"(?: to the)? (left|right|up|down|top left|top right|top"
                      r"|bottom left|bottom right|bottom|other side)(?: side)?" + _WRAP_TRAIL)

def parse(text: str, apps: dict[str, str] | None = None) -> Intent:
    """Transcript -> Intent; the cleaned transcript is the arg for the model kinds.

    `apps` (None -> list_apps()) is only consulted to resolve "open <name>.app"
    to a real installed app before the domain check.
    """
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    t = _MISHEARD_WAKE_RE.sub("", t, count=1).strip().rstrip(".?!").strip()
    low = t.lower()
    # Hide first: "get rid of it for now" is temporary, not the final clear.
    for rx, kind in ((_HIDE_RE, "hide"), (_CLEAR_RE, "clear"), (_SHOW_RE, "show"),
                     (_ASIDE_RE, "aside"), (_RESTORE_RE, "restore")):
        if rx.fullmatch(low):
            return Intent(kind)
    m = _MOVE_RE.fullmatch(low)
    if m:
        arg = m.group(1).replace(" ", "_")
        return Intent("move", {"top": "up", "bottom": "down"}.get(arg, arg))
    if _TIME_RE.match(low):
        return Intent("clock", "time")
    if _DATE_RE.match(low):
        return Intent("clock", "date")
    # Owner-data work before git_diff so history words win over diff words;
    # deterministic opens win over everything ("open github.com" is a site, not agent work).
    if not low.startswith("open "):
        sub = _agent_sub(low)
        if sub:
            return Intent("agent", t, sub)
    # "the changes since the last merge", "show the diff" — before ground,
    # whose leads ("highlight ...") would otherwise swallow these.
    if _DIFF_RE.search(low) or (_CHANGES_RE.search(low) and _MERGE_RE.search(low)):
        return Intent("git_diff", t)
    # Ground only for a UI thing on screen: a lead followed by a UI noun,
    # else "where is Paris" would ask the screen instead of the model.
    lead = _GROUND_LEAD_RE.search(low)
    if lead and _UI_NOUN_RE.search(low, lead.end()):
        return Intent("ground", t)
    if _LOOK_STRONG_RE.search(low):
        return Intent("look", t)
    look = _LOOK_LEAD_RE.search(low)
    if look and _LOOK_REF_RE.search(low, look.end()):
        return Intent("look", t)
    if low.startswith("open "):
        arg = t[5:].strip()
        name = _APP_NOISE_RE.sub("", arg).strip()
        if _match_app(name, apps if apps is not None else list_apps()):
            return Intent("open_app", name)
        host = arg.lower().partition("/")[0]
        kind = "open_site" if _HOST_RE.match(host) else "open_app"
        return Intent(kind, arg)
    return Intent("answer", t)


def _agent_sub(low: str) -> str:
    """Owner-data sub for the transcript, "" when it is not agent work."""
    if low.startswith(_AGENT_LEADS) and not _AGENT_GUARD_RE.search(low):
        return ""
    for sub, rx in _AGENT_SUBS:
        if rx.search(low):
            return sub
    return ""


def _clean(text: str) -> str:
    """Same transcript cleanup as parse: wake words and trailing punctuation off."""
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    return _MISHEARD_WAKE_RE.sub("", t, count=1).strip().rstrip(".?!").strip()


def _answer(text: str) -> str:
    """Whole-utterance form for yes/no/cancel: cleaned, lower case, punctuation as spaces."""
    t = re.sub(r"[,;:.!?]+", " ", _clean(text).casefold().replace("’", "'"))
    return " ".join(t.split())


def is_confirm(text: str) -> bool:
    """Whole utterance is a yes to the agent's confirmation question."""
    return bool(_CONFIRM_RE.fullmatch(_answer(text)))


def is_cancel(text: str) -> bool:
    """Whole utterance cancels the agent's work."""
    return bool(_CANCEL_RE.fullmatch(_answer(text)))


def is_no(text: str) -> bool:
    """Whole utterance says no to the agent's confirmation question."""
    return bool(_NO_RE.fullmatch(_answer(text)))


def looks_incomplete(text: str) -> bool:
    """True when the transcript trails off ("show me the last, uh"): more words are coming."""
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    if _FILLER_TAIL_RE.search(" " + t):  # "the last, huh?" is still mid-thought
        return True
    return bool(t) and not t.endswith((".", "?", "!")) and bool(_INCOMPLETE_RE.search(t))


def join_continuation(head: str, tail: str) -> str:
    """'show me the last, uh' + 'debugging session' -> 'show me the last debugging session'."""
    head = _FILLER_TAIL_RE.sub("", head.strip()).rstrip(" ,")
    return f"{head} {tail.strip()}".strip()


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


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def app_candidates(name: str, apps: dict[str, str]) -> list[str]:
    """Every installed app the spoken name could mean: the first tier with a hit wins.

    Tiers: exact, same words in any order, abbreviation ("vs code"), every word of
    the name is a word of the app, prefix. 2+ results = a tie (caller must not launch).
    """
    low = _APP_NOISE_RE.sub("", name.strip()).strip().lower()
    q = _tokens(low)
    if not q:
        return []
    if low in apps:
        return [apps[low]]
    compact = "".join(q)
    qset = set(q)
    tiers = (
        lambda k, t: set(t) == qset,
        lambda k, t: any("".join(w[0] for w in t[:i]) + "".join(t[i:]) == compact
                         for i in range(1, len(t) + 1)),
        lambda k, t: qset <= set(t),
        lambda k, t: k.startswith(low),
    )
    for hit in tiers:
        found = sorted({real for key, real in apps.items() if hit(key, _tokens(key))})
        if found:
            return found
    return []


def _match_app(name: str, apps: dict[str, str]) -> str | None:
    """Real app name for a transcript name; None = no match or a tie."""
    found = app_candidates(name, apps)
    return found[0] if len(found) == 1 else None


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
