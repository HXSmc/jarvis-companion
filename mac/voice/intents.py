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
    kind: str  # open_app | open_site | clear | clock | ground | look | git_diff | hunk | agent | answer | stop | card_size | sessions
    arg: str = ""
    sub: str = ""  # agent only: mail | calendar | notes | gh | git | code; git_diff: auto | merge


@dataclass
class MediaAsk:
    kind: str      # "image" | "video"
    subject: str   # cleaned search words; "" = asked, but no clear subject
    count: int     # 0 when subject == ""
    article: str = ""  # the leading article cut from the subject ("a"/"an"/"the"/"some"/""), for the spoken line only


@dataclass
class CardSizeAsk:
    action: str  # "max" | "restore"
    target: str  # cleaned target words, "" for restore


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
# Plain change requests ("highlight the changes") are a diff, not a look at the
# screen; whole-utterance anchors keep "highlight the URL" a look.
_CHANGES_ASK_RE = re.compile(r"^(?:(?:can|could) you )?(?:highlight|show(?: me)?|box|mark|point (?:to|at|out))(?: (?:the|my|all the|all my))?(?: (?:exact|latest|recent|new))? changes(?: (?:please|for me))?$")
_WHAT_CHANGED_RE = re.compile(r"^what(?:'s| is| has)? changed$")
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
# Hunk walk/why: whole utterances only, so history questions stay with the agent tier.
_HUNK_MOVE_RE = re.compile(r"^(?:(?:go to|show me|jump to) )?(?:the )?(next|previous) "
                           r"(?:change|hunk|one)(?: please)?$")
_HUNK_NEXT_RE = re.compile(r"^next(?: please)?$")
_HUNK_WHY_RE = re.compile(r"^(?:why did (?:this|that) change|why was (?:this|that) changed"
                          r"|explain (?:this|that) change)(?: please)?$")
# "go back" in the hunk walk; the service applies it only when a change list exists.
HUNK_BACK_RE = re.compile(r"^(?:go back(?: one)?|back|previous(?: one)?)(?: please)?$")
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
                       r"|blame|who changed|who wrote (?:this|that|it|these|those|the (?:code|line|lines|function|file|method|class|change))\b"
                       r"|what changed in|when did .* change|push my changes"
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
# Stop the turn: a superset of _CANCEL_RE (every cancel is a stop), plus silence asks.
STOP_PHRASES: tuple[str, ...] = (
    "stop", "stop it", "stop talking", "stop listening", "stop now", "ok stop", "okay stop",
    "that's all", "that's all for now", "that's it", "thanks that's all", "thank you that's all",
    "never mind", "nevermind", "be quiet", "quiet", "enough", "that's enough", "cancel",
    "cancel that", "cancel it", "forget it", "abort",
)
_STOP_RE = re.compile("(?:" + "|".join(re.escape(p) for p in STOP_PHRASES) + ")" + _POLITE)

# Overlay hide/show/aside/restore/move and the widened clear: whole-utterance
# phrases with an optional politeness wrapper ("can you hide it for now, please").
# Hide is checked before clear: "get rid of it" is temporary, "get rid of the cards" final.
_WRAP_LEAD = r"(?:(?:can|could|would) you |please |jarvis )*"
_WRAP_TRAIL = r"(?:,? (?:please|for me|for now|now|jarvis))*"
_OBJECT = r"(?:it|them|that|this|everything|all the cards|all of them|all of it|all|the overlay|the cards?)"
# STT writes a lone "hide" as the name "Hyde" (Mac live run 1); whole utterance only.
_HIDE_RE = re.compile(_WRAP_LEAD + r"(?:(?:hide|hyde)(?: " + _OBJECT + r")?|get rid of it|put (?:it|them|everything) away"
                      r"|get (?:it|that|this|them) off my screen|go away"
                      r"|get (?:it|this|that|them|everything) out of (?:my face|here)"
                      r"|make (?:it|that|them|everything) (?:disappear|go away))" + _WRAP_TRAIL)
_SHOW_RE = re.compile(_WRAP_LEAD + r"(?:show (?:it|them|everything|the overlay|the cards?)(?: again)?"
                      r"|bring (?:it|them|everything) back|unhide(?: " + _OBJECT + r")?|come back)" + _WRAP_TRAIL)
_CLEAR_RE = re.compile(_WRAP_LEAD + r"(?:clear(?: it| them| all| everything| the (?:screen|overlay|cards?))?"
                       r"|dismiss(?: it| them| that| all| everything)?"
                       r"|close (?:it|this|that|them|everything)|remove the cards|get rid of the cards)" + _WRAP_TRAIL)
_ASIDE_RE = re.compile(_WRAP_LEAD + r"(?:move (?:it|them) (?:out of the way|aside)"
                       r"|get (?:it|this|that|them|everything) out of (?:the|my) way"
                       r"|park (?:it|the cards))" + _WRAP_TRAIL)
_RESTORE_RE = re.compile(_WRAP_LEAD + r"put (?:it|them|the cards) back" + _WRAP_TRAIL)
# Direction last ("move it to the top left", "move to the left side"); "top"->up, "bottom"->down
# at the match site. No "the screen" object: "move the screen left" is not about the cards.
_MOVE_RE = re.compile(_WRAP_LEAD + r"(?:move|shift)(?: " + _OBJECT + r")?"
                      + r"(?: to the)? (left|right|up|down|top left|top right|top"
                      r"|bottom left|bottom right|bottom|other side)(?: side| corner)?" + _WRAP_TRAIL)
# Owner-spoken picture/video requests for the privacy path (media_request): the query is
# built only from the transcript, never by a model. Whole sentences, fullmatch.
_MEDIA_VERBS = r"(?:show|pull|pull up|find|get|fetch|bring up|give|display|search for|look up)"
_MEDIA_AMOUNT = r"(?: (?P<amt>a|an|one|some|a few|\d+|two|three|four|five|six))?"
_MEDIA_NOUN = r" (?P<noun>picture|photo|image|pic|video|clip|diagram)(?P<plural>s?)"
_MEDIA_REQUEST_RE = re.compile(r"(?:(?:can|could|would|will) you |please |jarvis,? )*"
                               + _MEDIA_VERBS + r"(?: me)?" + _MEDIA_AMOUNT + _MEDIA_NOUN
                               + r"(?: of (?P<subject>.+))?")
# Bare form "a picture of a dog": _MEDIA_NOUN carries the verb's leading space, so strip it.
_MEDIA_OF_RE = re.compile(r"(?:an? )?" + _MEDIA_NOUN.lstrip() + r" of (?P<subject>.+)")
# Subject tails that are politeness/timing, not search words.
_MEDIA_TAILS = ("as a test", "please", "for me", "right now", "now", "again",
                "jarvis", "sir", "thanks", "thank you")
_MEDIA_ARTICLE_RE = re.compile(r"(a|an|the|some)(?:\s+|$)")
_MEDIA_AMOUNTS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# Words that point at the here-and-now: a subject made only of these never searches.
_MEDIA_PRIVATE_WORDS = frozenset(
    "this that it these those here there my our screen file files code repo commit"
    " email mail note notes window tab page".split())

# Card enlarge/restore: whole-utterance asks with the usual politeness wrapper. An
# "on "/"upon " target is refused: "expand on that" is a question for the model.
_CARD_MAX_RE = re.compile(_WRAP_LEAD + r"(?:(?:maximi[sz]e|enlarge|zoom in on|full ?screen|blow up|expand)"
                          r" (?P<target>.+)"
                          r"|make (?P<make_target>it|that|this|that one|this one|the .+?) bigger)"
                          + _WRAP_TRAIL)
_CARD_RESTORE_RE = re.compile(_WRAP_LEAD + r"(?:make (?:it|that) smaller|shrink (?:it|that)"
                              r"|restore (?:it|that|the card)|back to normal|normal size"
                              r"|minimi[sz]e (?:it|that))" + _WRAP_TRAIL)
_CARD_TARGET_LEAD_RE = re.compile(r"^(?:the|that|this|my) ")
# Sessions read-only question; "what went wrong with X" carries the session name.
_SESSIONS_RE = re.compile(_WRAP_LEAD + r"(?:what are my sessions doing|is anything stuck"
                          r"|are any sessions waiting(?: for me| on me)?"
                          r"|what went wrong with (?P<name>.+)|how are my sessions(?: doing)?"
                          r"|session status)" + _WRAP_TRAIL)

def parse(text: str, apps: dict[str, str] | None = None) -> Intent:
    """Transcript -> Intent; the cleaned transcript is the arg for the model kinds.

    `apps` (None -> list_apps()) is only consulted to resolve "open <name>.app"
    to a real installed app before the domain check.
    """
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    t = _MISHEARD_WAKE_RE.sub("", t, count=1).strip().rstrip(".?!").strip()
    low = t.lower()
    if is_stop(text):
        return Intent("stop")
    ask = card_size_request(text)
    if ask:
        return Intent("card_size", ask.action, ask.target)
    m = _SESSIONS_RE.fullmatch(low.replace(",", ""))
    if m:
        return Intent("sessions", (m["name"] or "").strip())
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
    move = _HUNK_MOVE_RE.match(low)
    if move:
        arg = "next" if move[1] == "next" else "prev"
        return Intent("hunk", arg, arg)
    if _HUNK_NEXT_RE.match(low):
        return Intent("hunk", "next", "next")
    if _HUNK_WHY_RE.match(low):
        return Intent("hunk", "why", "why")
    # Owner-data work before git_diff so history words win over diff words;
    # deterministic opens win over everything ("open github.com" is a site, not agent work).
    if not low.startswith("open "):
        sub = _agent_sub(low)
        if sub:
            return Intent("agent", t, sub)
    # Plain change requests and "what changed" before ground/look, whose leads
    # ("highlight ...") would otherwise swallow them.
    if _CHANGES_ASK_RE.fullmatch(low) or _WHAT_CHANGED_RE.fullmatch(low):
        return Intent("git_diff", t, "auto")
    # "the changes since the last merge", "show the diff" — before ground,
    # whose leads ("highlight ...") would otherwise swallow these.
    if _DIFF_RE.search(low) or (_CHANGES_RE.search(low) and _MERGE_RE.search(low)):
        return Intent("git_diff", t, "merge" if _MERGE_RE.search(low) else "auto")
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


def _media_count(amt: str, plural: str, max_count: int) -> int:
    """Image count from the spoken amount: a number (1-6) capped, plural/"some" -> 4, else 1."""
    if amt.isdigit():
        return max(1, min(max_count, int(amt)))
    if amt in _MEDIA_AMOUNTS:
        return max(1, min(max_count, _MEDIA_AMOUNTS[amt]))
    if amt in ("some", "a few") or plural:
        return min(4, max_count)
    return 1


def media_request(text: str) -> MediaAsk | None:
    """The owner's spoken picture/video request, or None. Fixed code, never a model.

    Privacy path: when the conversation holds private text, the service may not let a
    model write a search query, so the subject is built only from this transcript — it
    can never contain words the owner didn't say. subject "" (count 0) = a clear ask
    with no searchable subject (deixis, screen words).
    """
    if len(text) > 300:
        return None
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    t = _MISHEARD_WAKE_RE.sub("", t, count=1).strip().lower()
    sentences = [s.rstrip(",").strip() for s in re.split(r"[.?!]+", t) if s.strip()][:3]
    from mac.voice import media_lookup  # lazy: importing intents must not pull httpx

    for sent in sentences:
        m = _MEDIA_REQUEST_RE.fullmatch(sent) or _MEDIA_OF_RE.fullmatch(sent)
        if not m:
            continue
        kind = "video" if m["noun"] in ("video", "clip") else "image"
        if not m["subject"]:
            return MediaAsk(kind, "", 0)
        count = 1 if kind == "video" else _media_count(m.groupdict().get("amt") or "",
                                                       m["plural"], media_lookup.MAX_COUNT)
        s = m["subject"]
        while True:  # trailing qualifiers ("as a test", "please") and commas/spaces off
            s = s.strip(" ,")
            tail = next((q for q in _MEDIA_TAILS if s == q or s.endswith(" " + q)), "")
            if not tail:
                break
            s = s[:-len(tail)].rstrip(" ,")
        article = ""
        lead = _MEDIA_ARTICLE_RE.match(s)
        if lead:
            article, s = lead[1], s[lead.end():]
        s = media_lookup.clean_query(s)
        if kind == "image" and m["noun"] == "diagram" and s:
            s += " diagram"  # the cap below counts it
        s = " ".join(s.split()[:6])[:media_lookup.MAX_QUERY].strip()
        if s and not s.isdigit() and not all(w in _MEDIA_PRIVATE_WORDS for w in s.split()):
            return MediaAsk(kind, s, count, article)
        return MediaAsk(kind, "", 0)
    return None


def card_size_request(text: str) -> CardSizeAsk | None:
    """Card enlarge/restore ask from the whole utterance, or None. Fixed code, never a model."""
    t = _clean(text).lower().replace(",", "")
    m = _CARD_MAX_RE.fullmatch(t)
    if m:
        target = (m["target"] or m["make_target"]).strip()
        if target.startswith(("on ", "upon ")):
            return None
        target = _CARD_TARGET_LEAD_RE.sub("", target)
        if target.endswith(" card"):
            target = target[:-5]
        return CardSizeAsk("max", target.strip())
    if _CARD_RESTORE_RE.fullmatch(t):
        return CardSizeAsk("restore", "")
    return None


def card_words(title: str) -> tuple[str, ...]:
    """Lower-case words of a title (apostrophes dropped, split on non-letters/digits)."""
    return tuple(_tokens(title.replace("'", "")))


# Spoken kind word -> the card types it can mean ("the picture" -> any image card).
_CARD_FLOW = frozenset({"flow", "bar"})
KIND_WORDS: dict[str, frozenset[str]] = {
    "diagram": _CARD_FLOW, "diagrams": _CARD_FLOW, "flow": _CARD_FLOW,
    "chart": _CARD_FLOW, "charts": _CARD_FLOW,
    "picture": frozenset({"image"}), "pictures": frozenset({"image"}),
    "image": frozenset({"image"}), "images": frozenset({"image"}),
    "photo": frozenset({"image"}), "photos": frozenset({"image"}), "pic": frozenset({"image"}),
    "video": frozenset({"video"}), "videos": frozenset({"video"}),
    "list": frozenset({"bar", "info"}), "changes": frozenset({"bar", "info"}),
}
# Words that never name a card: pointers and politeness around the real words.
_CARD_FILLERS = frozenset({"the", "a", "an", "that", "this", "these", "those", "my", "one",
                           "card", "please", "it"})


def match_card(target: str, cards: list[tuple[str, str, tuple[str, ...]]]) -> tuple[str, str] | None:
    """(id, how) for a spoken target against (id, type, title_words) cards, oldest first,
    searched newest first; None = no card answers to it."""
    words = [w for w in card_words(target) if w not in _CARD_FILLERS]
    kind: set[str] = set()
    for w in words:
        kind |= KIND_WORDS.get(w, frozenset())
    names = [w for w in words if w not in KIND_WORDS]
    if not names:
        if not kind:
            return (cards[-1][0], "newest") if cards else None
        for cid, ctype, _title in reversed(cards):
            if ctype in kind:
                return cid, "kind"
        return None
    hits = [c for c in reversed(cards) if set(names) <= set(c[2])]
    if kind:  # a spoken kind is a preference, not a requirement
        typed = [c for c in hits if c[1] in kind]
        if typed:
            hits = typed
    return (hits[0][0], "title") if hits else None


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


def is_stop(text: str) -> bool:
    """Whole utterance stops the turn: silence, a cancel, or both."""
    return bool(_STOP_RE.fullmatch(_answer(text)))


def is_no(text: str) -> bool:
    """Whole utterance says no to the agent's confirmation question."""
    return bool(_NO_RE.fullmatch(_answer(text)))


def is_hunk_back(text: str) -> bool:
    """Whole utterance is a hunk-walk "go back" (applied only with a change list)."""
    return bool(HUNK_BACK_RE.fullmatch(_clean(text).lower()))


def tail_hunk(text: str) -> str | None:
    """\"next\"/\"prev\" when the transcript ENDS with a whole hunk phrase, else None.

    The phrase may trail a dictation sentence ("... Next change"); a hunk phrase
    buried in a longer sentence ("the next change in the law") is not a command.
    """
    sentences = re.split(r"[.!?]\s+", _clean(text).lower())
    s = sentences[-1].strip().rstrip(".?!").strip()
    words = s.split()
    for k in range(2, 6):
        if k > len(words):
            break
        m = _HUNK_MOVE_RE.fullmatch(" ".join(words[-k:]))
        if m and (len(words) - k <= 3 or (len(sentences) > 1 and len(words) == k)):
            return "next" if m[1] == "next" else "prev"
    return None


QUESTION_WORDS_RE = re.compile(r"\b(?:what|why|how|when|where|who|which|can|could|would|should|is|are|do|does|did)\b")


# Talk to a group is never to Jarvis ("What are you people", Mac run 3 room speech).
_OTHERS_RE = re.compile(r"\b(?:you (?:people|guys|all)|y'all)\b")


def noise_words(text: str) -> int | None:
    """Word count for a 1-3 word utterance that is not a question or a reply, or a short one (6 words
    or fewer) that talks to a group, else None."""
    t = _clean(text)
    words = re.findall(r"[A-Za-z0-9']+", t)
    if 1 <= len(words) <= 6 and _OTHERS_RE.search(t.lower()):
        return len(words)
    if not 1 <= len(words) <= 3 or "?" in text:
        return None
    if QUESTION_WORDS_RE.search(t.lower()) or is_confirm(text) or is_no(text) or is_cancel(text):
        return None
    return len(words)


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
