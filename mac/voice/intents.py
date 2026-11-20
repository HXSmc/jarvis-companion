"""Deterministic overlay intents: transcript -> open an app/site, clear, ground, diff,
owner-data agent work, or answer.

First stop before any model call. "open X" either launches a real macOS app
(the name must match an entry from the app dirs — it never reaches `open` as
raw transcript text, and argv lists only, never a shell string) or a strictly
validated https URL. Everything needing the model (grounding, git diff, agent
work, plain questions) comes back as an Intent for the turn pipeline.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from mac.voice import note_find  # jauto3: spoken note names (no cycle: agent_policy imports none of this)
from mac.voice.auto_policy import OPEN_DOCS


@dataclass
class Intent:
    kind: str  # open_app | open_site | clear | clock | ground | look | git_diff | hunk | agent | answer | stop | card_size | card_remove | sessions | session | memory | workspace | geo_map | geo_roads | transit_next | transit_status | transit_journey | place_save | place_forget | video_ask | video_watch | draw | model_create | model_list | model_show | place | repo | new_topic | note_open  # jauto3
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


@dataclass
class CardRemoveAsk:
    target: str  # cleaned target words, or the pointer phrase ("that one")
    pointer: bool  # True: "that/this/it/..." -> the card the service already tracks
    card: bool = False  # True: the spoken target named a card/kind ("picture", "the ... card")


@dataclass
class SessionAsk:
    action: str  # "start" | "stop" | "answer"
    device: str  # start only: "mini_pc" | "mac" | "windows"; "" otherwise
    ref: str | None  # "this" | "name" | None
    name: str  # stop/answer: the session name words, "" when none
    words: str  # start: the request words; answer: the reply text; "" otherwise
    answer: str | None  # answer only: "yes" | "no" | "approve" | "text"


@dataclass
class MemoryAsk:
    action: str  # "add" | "list" | "forget_last" | "forget_about" | "forget_all"
    text: str  # the fact/rule words for add and forget_about, "" otherwise


@dataclass
class WorkspaceAsk:
    action: str  # "save" | "open" | "open_pages" | "list" | "delete"
    name: str  # the spoken workspace name, "" for open_pages and list


APP_DIRS = ("/Applications", "/System/Applications", "/System/Applications/Utilities", "~/Applications")

# Host: ordinary DNS labels only. Path (after the first /): a fixed safe
# charset — nothing that could turn into a scheme or a second argument.
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PATH_RE = re.compile(r"^[/A-Za-z0-9._~%-]*$")
URL_MAX = 2000  # jauto3
_URL_REST_RE = re.compile(r"^[/A-Za-z0-9._~%!$&'()*+,;=:@?#-]*$")  # jauto3: RFC 3986 characters only
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
# jauto2: "open https://..." is the site ask without its scheme (open_site always opens https).
_SCHEME_RE = re.compile(r"^https?://", re.I)
# jauto2: "open the note X" / "open my file X": a named document for the agent, never an app launch.
_DOC_TARGET_RE = re.compile(r"^(?:the|my|this|that|a)\s+(?:note|file|document|doc|pdf|folder|page|web\s?page"
                            r"|link|url|website)\s+\S", re.I)
_NOTE_WORD_RE = re.compile(r"\b(?:notes?|obsidian|vault)\b|\.(?:md|markdown)\b")
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
# Mac run r2: Daniel's "Hide everything." / "Hide the cards." came back as "Hi, everything" and
# "Hide the gods"; a "hi" counts only with one of these objects (a bare "hi" stays a greeting).
_HIDE_RE = re.compile(_WRAP_LEAD + r"(?:(?:hide|hyde)(?: " + _OBJECT + r")?"
                      r"|(?:hi|hide|hyde),? (?:everything|the cards?|the gods|them)"
                      r"|get rid of it|put (?:it|them|everything) away"
                      r"|get (?:it|that|this|them) off my screen|go away"
                      r"|get (?:it|this|that|them|everything) out of (?:my face|here)"
                      r"|make (?:it|that|them|everything) (?:disappear|go away))" + _WRAP_TRAIL)
# A bare "show" (whole utterance) is show too: Mac live run 2 sent it to the model.
_SHOW_RE = re.compile(_WRAP_LEAD + r"(?:show(?: (?:it|them|everything|the overlay|the cards?))?(?: again)?"
                      r"|bring (?:it|them|everything) back|unhide(?: " + _OBJECT + r")?|come back)" + _WRAP_TRAIL)
_CLEAR_RE = re.compile(_WRAP_LEAD
                       # "stop and clear everything": the barge-in lead rides along.
                       + r"(?:(?:stop|stop it|stop talking)(?: and| then|,)? )?"
                       r"(?:clear(?: it| them| all| everything| the (?:screen|overlay|cards?))?"
                       r"|dismiss(?: it| them| that| all| everything)?"
                       r"|close (?:it|this|that|them|everything)|remove the cards|get rid of the cards"
                       r"|remove everything|remove all(?: the cards| of them)?"
                       r"|get rid of everything|close all(?: the)? cards|delete everything)" + _WRAP_TRAIL)
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
# loose=True's net: any "<noun> of <subject>" inside a sentence that didn't fullmatch.
_MEDIA_LOOSE_RE = re.compile(
    r"\b(?P<noun>picture|photo|image|video|diagram|clip)(?P<plural>s)? of (?P<subject>.+)")
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
# Remove ONE card ("close the phone diagram"); fixed code, never a model. Whole utterance.
# "clear" joins for one NAMED card only (the stop set below refuses its pointer and
# whole-overlay forms); _CLEAR_RE still wins first in the fast path.
_CARD_REMOVE_RE = re.compile(r"(?:(?:remove|close|dismiss|get rid of|take away|take down|lose|clear|delete)"
                             r" (?P<t1>.+)|take (?P<t2>.+) (?:away|down))")
# A trailing overlay/screen place is politeness, not part of the name: cut before the
# blocked-word check ("from the overlay" would otherwise block the whole ask).
_CARD_PLACE_RE = re.compile(r"[ ](?:from|off|on) (?:the|my) (?:overlay|screen)$")
# "clear" never names one card with these alone: the whole-overlay and pointer forms.
_CLEAR_STOP = frozenset({"overlay", "screen", "last", "newest"})
# Words that make an ask about many or all cards ("clear the whole screen", "every card").
_WHOLE_SET = frozenset({"whole", "entire", "every", "each", "all", "both", "everything"})
_CARD_REMOVE_POINTERS = frozenset({"that", "this", "it", "that one", "this one", "that card",
                                    "this card", "the card", "the last card",
                                    "the newest card", "the last one"})
# These targets belong to today's kinds (highlighting, boxes, apps, windows): never a card
# remove; plural "cards"/"them"/"everything"/"all" is the clear, not a one-card ask.
_CARD_REMOVE_BLOCKED = frozenset(
    "highlight highlights box boxes arrow arrows outline outlines app apps window windows "
    "tab tabs cards them everything all "
    # A place or topic after the target ("the picture from my notes", "the diagram in
    # Preview") is owner-data or app work for the agent, never a card on screen.
    "from in on about into inside onto at to off within under".split())
_SESS_NAME = r"(?!(?:it|that|this|my|your|a|an)\b)[a-z0-9][\w'.-]*(?: [\w'.-]+){0,3}"
# Sessions read-only question; "what went wrong with X" carries the session name.
_SESSIONS_RE = re.compile(_WRAP_LEAD + r"(?:what are my sessions doing|is anything stuck"
                          r"|are any sessions waiting(?: for me| on me)?"
                          r"|what went wrong with (?P<name>.+)|how are my sessions(?: doing)?"
                          r"|session status"
                          r"|(?P<itref>what is it doing|how is it going|what's it doing)"
                          # jfollow11c C1: what one session is doing or did (the name filters by title)
                          r"|(?P<last>what did my last session do)"
                          r"|what(?: is|'s|s) (?:the )?session (?P<sname>" + _SESS_NAME + r") doing"
                          r"|what(?: is|'s|s) the (?P<sname2>" + _SESS_NAME + r") session doing"
                          r"|what did (?:the )?(?P<sname3>" + _SESS_NAME + r") session do)"
                          + _WRAP_TRAIL)
# jfollow6 D: a wider net of read-only sessions questions/asks ("what's running on my
# companion"); matched after session/memory/workspace, before the hide/clear verbs, so
# their phrases keep their kinds. Whole utterance, commas off.
# jfollow7 S: an adjective before "sessions", the article before the device optional, more devices (mac, router, STT forms of mini PC).
_COMPANION = (r"(?:(?:my|the|our) )?(?:(?:\w+(?:'s|s')? )?companion(?: app)?|(?:mini|many) ?(?:p ?c|b ?c|piece)"
              r"|minipc|mac|windows(?: pc)?|router)")
# jfollow10g: one word naming where or which sessions ("router sessions", "done sessions"); router,
# companion and app mean every session on the app.
_SESSIONS_NOUN = (r"(?:active|running|open|live|current|done|finished|completed|ended|router|app|mac|windows"
                  r"|(?:\w+(?:'s|s')? )?companion|(?:mini|many) ?(?:p ?c|b ?c|piece)|minipc)")
_SESSIONS_PREP = r"(?:on|in|through|via)"
# R1: "how" questions keep base's article rule ("how are the sessions"), so "how do I check
# sessions" / "how long are sessions" stay answers.
_SESSIONS_LEAD = (r"(?:check|show|list|see|tell me|give me|what's|whats|what|which|how many|any"
                  r"|are there|are|is|do i have|can you tell me|can you show me|i want to see"
                  r"|i'd like to see|i would like to see)")
_SESSIONS_TAIL = (r"(?: (?:that |which )?(?:are |is |were )?(?:doing|going|running|open|active"
                  r"|done|finished|completed|ended|ran))?"
                  r"(?: (?:from )?(?:today|yesterday|this week|(?:this )?past week)"
                  r"| (?:in |from )?the (?:last|past) (?:7|seven) days| right now| now| ok| okay| do i have)?"
                  r"(?: " + _SESSIONS_PREP + r" " + _COMPANION + r")?")
_SESSIONS_TOPIC_RE = re.compile(
    # R1: never with a start/stop verb anywhere ("check and stop all sessions" is no listing).
    r"(?!.*\b(?:start|stop|end|kill|close|cancel|spin up)\b)(?:"
    + _SESSIONS_LEAD + r"[\w' ]{0,16}?(?:(?:my|the|our) )?(?:" + _SESSIONS_NOUN + r" )?sessions"
    + _SESSIONS_TAIL +
    r"|what (?:did|have) (?:my|the|our) (?:" + _SESSIONS_NOUN + r" )?sessions (?:do|done|been doing)"
    + _SESSIONS_TAIL +
    r"|(?:how's|hows|how)[\w' ]{0,16}?(?:my|the|our) (?:(?:active|running|open|live|current) )?sessions"
    + _SESSIONS_TAIL +
    r"|(?:(?:" + _SESSIONS_LEAD + r"|how's|hows|how) )?(?:any )?(?:(?!open )" + _SESSIONS_NOUN + r" )?sessions? (?:are |is )?"
    r"(?:(?:running|active|open|going)(?: " + _SESSIONS_PREP + r" " + _COMPANION + r")?"
    r"|" + _SESSIONS_PREP + r" " + _COMPANION + r")"
    r"|what(?:'s| is| are)? (?:running|active) " + _SESSIONS_PREP + r" " + _COMPANION +
    r"|status of (?:my|the|our) sessions)")
# jfollow6 C1: "can you remove a single card?" is a how-to question, not a remove.
_CARD_HELP_RE = re.compile(
    r"(?:can you|could you|can't you|cannot you|can you not|could you not|how do i|how can i"
    r"|is there a way to|are you able to) (?:not )?"
    r"(?:remove|close|dismiss|get rid of|clear) (?:a|one|a single|single|just one) card"
    r"(?: from the overlay| from the screen| for me| please)?")

# Owner asks over the deterministic session/memory/workspace stores; matched on the
# cleaned transcript (original case kept for the extracted words), never a model.
SESSION_WORDS_MAX = 300

_SESSION_START_RE = re.compile(_WRAP_LEAD + r"(?:start|open|spin up) (?:a )?(?:new )?session"
                               r"(?: on (?:the )?(?P<dev>mini ?pc|mac|windows(?: pc)?))?"
                               r"(?: and| to| about| for)? (?P<words>.+)", re.I | re.S)
# Mac run r2: "stop the session called voice test write hello file" fell through to the model.
_SESSION_STOP_RE = re.compile(r"stop (?:the )?(?P<name>.+?) session"
                              r"|stop (?:the )?session (?:called |named )?(?P<name2>.+)",
                              re.I | re.S)
# D1, Mac run r2: "open the note Projects in the Secrets folder" went to the app opener and a
# notes ask to the model, so the fixed refusal was never heard. Any ask naming the vault's
# Secrets folder gets the host line, before any app, model or tool (stricter, never looser).
_VAULT_SECRETS_RE = re.compile(r".*\b(?:secrets? (?:folder|directory|notes?)"
                               r"|(?:in|from|inside|under) (?:my |the |your )?secrets)\b.*", re.I | re.S)
# jfollow10a: the owner's voice switch for running agent commands without the spoken Confirm.
# Whole utterance only; "stop" / "stop asking" alone never match ("commands" is required).
_CMDS = r"(?:running |you run )?(?:(?:a|any|every|each) )?commands?"
_BEFORE = r"before (?:" + _CMDS + r"|(?:running |you run )?anything)"
_CONFIRMS = r"(?:command )?(?:confirmations|confirm)"
_AUTO_OFF_RE = re.compile(_WRAP_LEAD + r"(?:(?:always )?ask (?:me )?" + _BEFORE + r"|always ask (?:me )?first"
                         r"|(?:turn|switch) on " + _CONFIRMS + r"|stop running commands without asking)"
                         + _WRAP_TRAIL)
_AUTO_ON_RE = re.compile(_WRAP_LEAD + r"(?:(?:stop|quit) asking (?:me )?(?:" + _BEFORE + r"|to confirm " + _CMDS + r")"
                        + r"|(?:don't|do not) ask (?:me )?" + _BEFORE
                        + r"|(?:turn|switch) off " + _CONFIRMS
                        + r"|run commands without asking(?: me)?|(?:go|switch to) full auto)"
                        + _WRAP_TRAIL)
_SESSION_ANSWER_RE = re.compile(
    r"(?:tell (?P<tname>the .+? session) (?P<tyn>yes|no|to go ahead)"
    r"|approve (?P<aname>the .+? session)"
    r"|tell (?:(?P<sub>the .+? session)|it|that session)[:,]? (?P<words>.+)"
    r"|answer (?P<asub>the .+? session)[:,]? (?P<awords>.+))", re.I | re.S)
_SESSION_DEVICES = {"minipc": "mini_pc", "mac": "mac", "windows": "windows",
                    "windowspc": "windows"}
_SESSION_THIS_RE = re.compile(r"this session|that session|the quiet session|the stuck session", re.I)

_MEMORY_ADD_RES = (re.compile(r"remember that (?P<fact>.+)", re.I | re.S),
                   re.compile(r"remember this[:,] (?P<fact>.+)", re.I | re.S),
                   re.compile(r"remember[:,] (?P<fact>.+)", re.I | re.S),
                   re.compile(r"note that (?P<fact>.+)", re.I | re.S))
_MEMORY_RULE_RE = re.compile(r"from now on (?P<rule>.+)", re.I | re.S)
_MEMORY_LIST_RE = re.compile(r"what (?:do you remember|have you remembered)", re.I)
_MEMORY_FORGET_ABOUT_RE = re.compile(r"forget what i said about (?P<words>.+)", re.I | re.S)
_MEMORY_FORGET_ALL_RE = re.compile(r"forget everything(?: you remember)?", re.I)

_WORKSPACE_SAVE_RE = re.compile(
    r"save (?:these|this|them|everything)?(?: (?:pages and overlays|pages and cards"
    r"|overlays|cards|pages))? as (?P<name>.+)", re.I | re.S)
_WORKSPACE_OPEN_PAGES_RE = re.compile(r"open (?:the|those|these|the saved) pages", re.I)
_WORKSPACE_OPEN_RE = re.compile(r"(?:open|bring back|show(?: me)?) (?:the |my |our )?(?P<name>.+)",
                                re.I | re.S)
_WORKSPACE_LIST_RE = re.compile(
    r"what have i saved|what (?:workspaces|research) do i have|list my (?:workspaces|research)",
    re.I)
_WORKSPACE_DELETE_RE = re.compile(r"delete (?:the |my |our )?(?P<name>.+)", re.I | re.S)
_WORKSPACE_FILLERS = frozenset({"our", "my", "the", "research", "workspace"})

def fast_forms(text: str) -> list[str]:
    """The whole text, plus its last sentence for short 2-3 sentence dictation
    ("That's nice. Clear"): a fast-path kind may hide behind the small talk."""
    forms = [text]
    if len(text) <= 200:
        pieces = [p.strip() for p in re.split(r"[.?!]+", text) if p.strip()]
        if 2 <= len(pieces) <= 3:
            forms.append(pieces[-1])
    return forms


# jfollow11c D4: start the conversation over (whole short utterance only).
_NEW_TOPIC_RE = re.compile(_WRAP_LEAD + r"(?:(?:let's |lets |ok |okay )?(?:new topic|start over|start again"
                           r"|(?:start )?(?:a )?new conversation|change (?:the )?topic))" + _WRAP_TRAIL)


@dataclass
class RepoAsk:
    action: str  # "read" (the README) | "search"
    name: str    # the owner's words for the repo; fixed code maps them to a listed name
    query: str   # search words from the owner's words ("" for read)


_README = r"\b(?:readme(?:\.md)?|read me)\b"
_REPO_README_RE = re.compile(_README + r"(?: file)? (?:of|for|in|from) (?:the |my )?"
                             r"(?P<name>[a-z0-9][\w.-]*(?: [\w.-]+){0,4}?)"
                             r"(?: (?:repo|repository))?(?: (?:say|says|said|file|contain|contains))?$")
_REPO_LEAD = re.compile(r"\b(?:where|which file|what file|find|search|look|grep|read|in which)\b")
# A change, a run or a session ask is never a read-only repo question.
_REPO_CHANGE = re.compile(r"\b(?:fix|edit|update|change|changed|write|rewrite|add|remove|delete|create|commit"
                          r"|push|pull|merge|refactor|rename|move|start|stop|run|install|deploy|open|diff"
                          r"|session|sessions|clone|build|test)\b")
_REPO_STOP = frozenset("the a an this that my our your in of for from on at to is it its which what where and or "
                       "with whole same current local code git github".split())
_REPO_NOT_NAME = _REPO_STOP | frozenset("repo repository repos project projects codebase app here these those".split())
_QUERY_STOP = _REPO_STOP | frozenset(
    "is are was be handled handle handles defined define implemented implement done located lives live kept set up "
    "used use found does do did how show me find search look grep read please jarvis hey can you could would tell "
    "about whats where's wheres file files line lines repo repository repos part place thing get gets readme "
    "say says there any all".split())
_QUERY_WORD = re.compile(r"[A-Za-z0-9_.-]{2,40}")


def repo_query(low: str) -> str:
    """Up to 6 search words from the owner's words: stop words off, each [A-Za-z0-9_.-]{2,40}."""
    out: list[str] = []
    for w in re.findall(r"[a-z0-9_.-]+", low):
        w = w.rstrip(".")
        if w and w not in _QUERY_STOP and _QUERY_WORD.fullmatch(w) and w not in out:
            out.append(w)
    return " ".join(out[:6])


def repo_request(text: str) -> RepoAsk | None:
    """jfollow11c B6: "what does the README of X say" (read) or "where is Y in the X repo" (search)."""
    low = _clean(text).lower().replace(",", "")
    # R3 O1: Apple STT writes "jarvis's companion public", and (live run 3) "jarvis' companion public"
    low = re.sub(r"(?<=\w)['\u2019](?:s\b|(?=\s|$))", "", low)
    if _REPO_CHANGE.search(low):
        return None
    m = _REPO_README_RE.search(low)
    if m:
        name = m["name"].strip()
        # R3: "the readme of this repo / it / my project" names no listed repo (README reads skip the pick)
        return None if set(name.split()) <= _REPO_NOT_NAME else RepoAsk("read", name, "")
    words = low.split()
    for i in range(len(words) - 1, -1, -1):
        if words[i] in ("repo", "repository", "repo's"):
            j = i
            while j > 0 and i - j < 4 and words[j - 1] not in _REPO_STOP:
                j -= 1
            if j == i:
                return None
            if not _REPO_LEAD.search(low):
                return None
            rest = " ".join(words[:j] + words[i + 1:])
            if re.search(_README, rest):
                return RepoAsk("read", " ".join(words[j:i]), "")
            query = repo_query(rest)
            # "where is the X repo": nothing to search for, so not a repo search (BASE kinds keep it)
            return RepoAsk("search", " ".join(words[j:i]), query) if query else None
    return None


def first_form(text: str, fn):
    """fn(form) for the first form in fast_forms(text) that returns non-None, else None.
    The service re-parses raw speech per kind; this keeps it agreeing with parse."""
    for form in fast_forms(text):
        result = fn(form)
        if result is not None:
            return result
    return None


def _form_hit(rx: re.Pattern, low: str) -> re.Match | None:
    """The first fullmatch of a fixed-phrase regex across the matcher's forms."""
    for form in _match_forms(low):
        m = rx.fullmatch(form)
        if m:
            return m
    return None


# jfollow10c: "draw me X" / "sketch X" / "make me a labelled diagram of X" (whole utterance).
_DRAW_NOUN = r"(?:picture|drawing|sketch|diagram|illustration)"
_DRAW_ADJ = r"(?:(?:simple|quick|rough|clear|detailed|labell?ed|little|small|big|nice)\s+)*"
_DRAW_RE = re.compile(
    r"(?:(?:can|could|would|will) you\s+|please\s+)?(?:draw|sketch|illustrate|diagram)(?:\s+(?:me|us))?"
    r"(?:\s+(?:a|an)\s+" + _DRAW_ADJ + _DRAW_NOUN + r"\s+of)?\s+(?P<subject>.+)"
    r"|(?:make|create|give) me (?:a|an)\s+" + _DRAW_ADJ + _DRAW_NOUN + r"\s+of\s+(?P<made>.+)", re.I | re.S)
_DRAW_CHART_RE = re.compile(r"\b(?:chart|charts|graph|graphs|plot|plots|pie|histogram)\b", re.I)


def draw_request(text: str) -> str | None:
    """The subject of a drawing ask ("" = it only points: "draw that"), or None. A chart or graph
    is an answer-pool chart card, never a drawing; "show me a diagram of X" stays a picture search."""
    t = destutter(_strip_tail(strip_lead(_clean(text)))).strip().rstrip(".?!,").strip()
    if len(t) > 300:
        return None
    m = _DRAW_RE.fullmatch(t)
    if not m:
        return None
    subject = (m["subject"] or m["made"] or "").strip()
    if _DRAW_CHART_RE.search(subject):
        return None
    words = _tokens(subject.lower())
    if all(w in _MEDIA_PRIVATE_WORDS or w in ("a", "an", "the", "one") for w in words):
        return ""
    return subject


# jfollow10d: 3D objects. Whole utterance only; "show me the cards" / "open notes" keep their kinds.
_3D = r"(?:3d|3-d|3 d|three d|three-d)"
_MODEL_CREATE_RE = re.compile(r"(?:(?:can|could|would) you )?(?:please )?"
                              r"(?:create|make|model|build|design|generate|render) (?:me )?(?:a |an )?" + _3D
                              + r"(?: (?:object|model|thing))?(?P<rest>(?:[:, ].*)?)")
_MODEL_NAME_RE = re.compile(r"(?P<desc>.*?)(?:(?:^| ?,? )(?:and )?(?:call(?:ed)? it|called|named) (?P<name>.+))?")
_MODEL_LIST_RE = re.compile(r"(?:what " + _3D + r" (?:objects|models) (?:do i have|have i got|are there|have you made)"
                            r"|(?:list|show)(?: me)? (?:my |the )?" + _3D + r" (?:objects|models))")
_MODEL_SHOW_RE = re.compile(r"(?:(?:can|could|would) you )?(?:please )?"
                            r"(?:pull up|show(?: me)?|bring up|open|display|load) (?:the |my )?(?P<name>.+?)"
                            r"(?P<three> " + _3D + r" (?:object|model))?")


def model_request(text: str, model_names: tuple[str, ...] = ()) -> Intent | None:
    """A 3D object ask: model_create (arg = description, sub = name, or sub "ask" when no
    description), model_list, or model_show (arg = the name words) when the name is a known object
    (slug or alias) or the words say "3D". None = not a 3D ask."""
    low = re.sub(r",? please$", "", _clean(text).lower())  # "... a mug please" never names a "mug please"
    m = _MODEL_CREATE_RE.fullmatch(low)
    if m:
        rest = re.sub(r"^[\s:,]+(?:of )?", "", m["rest"]).strip()
        parts = _MODEL_NAME_RE.fullmatch(rest)
        desc = (parts["desc"] if parts else rest).strip(" ,:")
        name = ((parts["name"] or "") if parts else "").strip(" ,:")
        desc = desc or name  # "create a 3D object called test mug": the name is all we have
        return Intent("model_create", desc, name) if desc else Intent("model_create", "", "ask")
    if _MODEL_LIST_RE.fullmatch(low.replace(",", "")):
        return Intent("model_list")
    m = _MODEL_SHOW_RE.fullmatch(low)
    if m and (m["three"] or re.search(_3D, m["name"]) is None and _model_key(m["name"]) in model_names):
        return Intent("model_show", m["name"].strip())
    return None


def model_near(text: str, model_names: tuple[str, ...]) -> Intent | None:
    """A show ask whose name is a speech-to-text near miss of a known object ("bring up the test
    cue" for test-cube; Mac live run). Only for an utterance that would otherwise be a free answer,
    a name of two or more words where exactly one word was misheard (one-word asks like "pull up the
    weather" stay answers whatever the vault holds), at the service's close-match cutoff (0.8)."""
    m = _MODEL_SHOW_RE.fullmatch(re.sub(r",? please$", "", _clean(text).lower()))
    if not m or m["three"] or re.search(_3D, m["name"]):
        return None
    key = _model_key(m["name"])
    words = key.split("-")
    near = [n for n in model_names if len(n.split("-")) == len(words) > 1
            and sum(a != b for a, b in zip(n.split("-"), words)) == 1]
    if not difflib.get_close_matches(key, near, n=1, cutoff=0.8):
        return None
    return Intent("model_show", m["name"].strip())


def _model_key(words: str) -> str:
    """models3d.slugify without the import (same rule): the key model_names holds."""
    out = [w for w in re.split(r"[^a-z0-9]+", words.lower()) if w and w not in ("a", "an", "the", "my")]
    return "-".join(out)[:40].strip("-") or "object"


# jauto3: spoken note names, front ("open the no llm switch ...") and tail ("open the llm
# switch note") forms. Both run on the punctuation-stripped whole utterance: "Open the node.
# LLM Switch ..." is one spoken name split by the sentence detector, so no last-sentence try.
_NOTE_FRONT_RE = re.compile(r"^open (?:(?:the|my) )?(?P<w>notes?|nodes?|no|not) (?P<name>.+)$")
_NOTE_TAIL_RE = re.compile(r"^open (?:(?:the|my) )?(?P<name>.+?) (?P<w>note|notes|node)$")


def _note_intent(text: str, note_names: tuple[str, ...]) -> Intent | None:
    """A spoken "open the note <name>" that matches a real vault note; else None."""
    if not note_names:
        return None
    n = re.sub(r"\s+", " ", re.sub(r"[.?!,;:]+", " ", strip_lead(_clean(text)).lower())).strip()
    if not n.startswith("open "):
        return None
    if re.search(r"\bsecrets?\b", n):  # jauto3: the Secrets refusal keeps priority
        return None
    m = _NOTE_FRONT_RE.match(n) or _NOTE_TAIL_RE.match(n)
    if not m:
        return None
    name = m["name"].strip()
    if set(note_find.tokens(name)) <= {"app", "apps", "application"}:  # "open my notes folder", "open note app"
        return None
    if m["w"] == "note":  # the owner typed/said the exact singular: honor the stem as written
        return Intent("note_open", name, "literal")
    # a misheard plural ("no", "node", "notes") opens the note only on a real token match
    if note_find.find(note_names, name, literal=False):
        return Intent("note_open", name)
    return None


def _fast_intent(text: str, apps: dict[str, str] | None,
                 ws_names: tuple[str, ...], last: bool = False,
                 place_names: tuple[str, ...] = (),
                 note_names: tuple[str, ...] = ()) -> Intent | None:
    """Every fixed fast-path kind for one utterance form; None = this form matches none.
    `last` (the last-sentence pass) skips clock and hunk: those stay whole-utterance
    ("Hi, I'm stuck. What time is it" is a question; hunk tails belong to tail_hunk)."""
    if is_stop(text):
        return Intent("stop")
    # First after stop: "can you remove one card" is a how-to question, not a remove.
    if _CARD_HELP_RE.fullmatch(_clean(text).lower().replace(",", "")):
        return Intent("card_help")
    ask = card_size_request(text)
    if ask:
        return Intent("card_size", ask.action, ask.target)
    if not last:
        model = model_request(text)  # no names here: a bare vault name is tried in parse, after fixed kinds
        if model is not None:
            return model
    t = _clean(text)
    low = t.lower()
    subject = draw_request(text)
    if subject is not None:
        return Intent("draw", subject)
    m = _form_hit(_SESSIONS_RE, low.replace(",", ""))
    if m:
        name = m["name"] or m["sname"] or m["sname2"] or m["sname3"] or ""
        return Intent("sessions", name.strip(), "ref" if m.group("itref") else "last" if m.group("last") else "")
    if _form_hit(_NEW_TOPIC_RE, low.replace(",", "")):
        return Intent("new_topic")
    ask = session_request(text)
    if ask:
        return Intent("session", ask.action)
    ask = memory_request(text)
    if ask:
        return Intent("memory", ask.action)
    if not last and _form_hit(_AUTO_OFF_RE, low.replace(",", "")):  # whole utterance only
        return Intent("auto_mode", "off")
    if not last and _form_hit(_AUTO_ON_RE, low.replace(",", "")):
        return Intent("auto_mode", "on")
    if _form_hit(_VAULT_SECRETS_RE, low):
        return Intent("vault_secrets")
    if not last:  # jfollow10b: whole-utterance kinds, before workspace and open
        hit = _place_match(text, place_names, apps, take_me=False)  # jfollow10f: before the map check
        if hit and hit[0] == "place":
            return Intent("place", hit[1]["mode"])
        if hit:
            return Intent("geo_map", hit[1])
        geo = geo_request(text, place_names)
        if geo:
            return Intent(geo.kind, geo.b or geo.a, geo.mode)
        vid = video_request(text)
        if vid:
            return Intent(vid.kind, vid.question, vid.video_id)
    ask = workspace_request(text, ws_names)
    # jauto3: a spoken note name ("open the no llm switch benchmark") beats the workspace
    # suffix guess, but never a saved workspace name whose own words it repeats.
    if not last and note_names and not (
            ask and ask.action in ("open", "delete")
            and any((set(ask.name.split()) - _WORKSPACE_FILLERS) <= set(_ws_name(n).split())
                    for n in ws_names)):
        hit = _note_intent(text, note_names)
        if hit is not None:
            return hit
    # An installed app or a site wins over a saved name sharing a word ("open notes").
    # "delete the cat card" is a card ask even when a saved name shares the words; a saved
    # "shopping list" or "meeting notes" (kind words) stays a workspace delete.
    if ask and ask.action == "delete" and "card" in ask.name.split():
        ask = None
    if ask and not (ask.action == "open" and _open_intent(t, apps) is not None):
        return Intent("workspace", ask.action)
    repo = repo_request(t)
    if repo is not None:
        return Intent("repo", repo.name, repo.action)
    if _form_hit(_SESSIONS_TOPIC_RE, low.replace(",", "")):
        return Intent("sessions", "", "")
    # Hide first: "get rid of it for now" is temporary, not the final clear.
    for rx, kind in ((_HIDE_RE, "hide"), (_CLEAR_RE, "clear"), (_SHOW_RE, "show"),
                     (_ASIDE_RE, "aside"), (_RESTORE_RE, "restore")):
        if _form_hit(rx, low):
            return Intent(kind)
    ask = card_remove_request(text)
    # Names-only targets ("close Safari") stay with today's kinds in parse: the service
    # upgrades them to a card remove only when a card answers to the name.
    if ask and (ask.pointer or ask.card):
        return Intent("card_remove", "pointer" if ask.pointer else "target", ask.target)
    m = _form_hit(_MOVE_RE, low)
    if m:
        arg = m.group(1).replace(" ", "_")
        return Intent("move", {"top": "up", "bottom": "down"}.get(arg, arg))
    if not last:
        if _form_hit(_TIME_RE, low):
            return Intent("clock", "time")
        if _form_hit(_DATE_RE, low):
            return Intent("clock", "date")
        move = _form_hit(_HUNK_MOVE_RE, low)
        if move:
            arg = "next" if move[1] == "next" else "prev"
            return Intent("hunk", arg, arg)
        if _form_hit(_HUNK_NEXT_RE, low):
            return Intent("hunk", "next", "next")
        if _form_hit(_HUNK_WHY_RE, low):
            return Intent("hunk", "why", "why")
    return None


def parse(text: str, apps: dict[str, str] | None = None,
          workspace_names: tuple[str, ...] | None = None,
          place_names: tuple[str, ...] | None = None,
          model_names: tuple[str, ...] | None = None,
          note_names: tuple[str, ...] | None = None) -> Intent:
    """Transcript -> Intent; the cleaned transcript is the arg for the model kinds.

    Fast-path kinds run on the whole text, then once more on its last sentence
    (fast_forms); the model kinds always see the whole cleaned transcript.
    `apps` (None -> list_apps()) is only consulted to resolve "open <name>.app"
    to a real installed app before the domain check. `workspace_names` (saved
    workspace names) lets "open <name>" resolve to a workspace ask; `model_names` (3D
    object slugs + aliases) lets "pull up <name>" resolve to a 3D object;
    `note_names` (vault-relative note paths from note_find.scan) lets a spoken
    "open the note <name>" resolve to a note open.
    """
    forms = fast_forms(text)
    for i, form in enumerate(forms):
        intent = _fast_intent(form, apps, workspace_names or (), last=i > 0, place_names=place_names or (),
                              note_names=note_names or ())
        if intent is not None:
            return intent
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    t = _MISHEARD_WAKE_RE.sub("", t, count=1).strip().rstrip(".?!").strip()
    low = t.lower()
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
        hit = _open_intent(t, apps)
        if hit:
            return hit
    # A bare 3D name ("pull up the test mug") comes from the vault (folders, mini PC-written
    # aliases), so it is tried only after every fixed kind and a real app or site, and never
    # for a stop phrase: a name like "stop" must not swallow a spoken stop ("load stop").
    model = model_request(text, model_names) if model_names and not is_stop_family(text) else None
    if model is not None:
        return model
    if low.startswith("open "):
        target = t[5:].strip()
        # jauto2: a note, document, path or URL nothing installed answers to goes to the agent (a tie still asks)
        if _doc_target(target) and not app_candidates(target, apps if apps is not None else list_apps()):
            if re.search(r"\bsecrets\b", low):  # "open Secrets/x.md": the Secrets refusal, never an agent hunt
                return Intent("vault_secrets")
            return Intent("agent", t, "notes" if _NOTE_WORD_RE.search(low) else _agent_sub(low))
        return Intent("open_app", target)
    if is_stop_family(text):  # last: every fixed phrase ("stop clear" = clear) wins over it
        return Intent("stop")
    near = model_near(text, model_names) if model_names else None  # a misheard name, before a free answer
    if near:
        return near
    hit = _place_match(text, place_names or (), apps, take_me=True)  # "take me to <x>": last, never steals a kind
    if hit:
        return Intent("place", hit[1]["mode"]) if hit[0] == "place" else Intent("geo_map", hit[1])
    return Intent("answer", t)


def _safe_url(url: str) -> str | None:
    """A full typed http(s) URL re-checked part by part and normalized to https; None = refuse.

    The rest after the bare host (no userinfo, no port, no spaces) may only hold RFC 3986
    characters, so a query or hash is fine but quotes, control bytes and shell text are not.
    """
    if len(url) > URL_MAX or not url.isascii():
        return None
    m = _SCHEME_RE.match(url)
    if not m:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    netloc = parts.netloc
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not _HOST_RE.match(netloc.lower()):
        return None
    rest = url[m.end() + len(netloc):]
    if rest and rest[0] not in "/?#":
        return None
    if not _URL_REST_RE.match(rest):
        return None
    out = "https://" + netloc + rest  # jauto3: http upgrades to https like every site open
    return out if len(out) <= URL_MAX else None


def _open_intent(t: str, apps: dict[str, str] | None) -> Intent | None:
    """"open <x>" resolved to an installed app or a site, else None."""
    if not t.lower().startswith("open "):
        return None
    arg = t[5:].strip()
    name = _APP_NOISE_RE.sub("", arg).strip()
    if _match_app(name, apps if apps is not None else list_apps()):
        return Intent("open_app", name)
    site = _SCHEME_RE.sub("", arg)  # jauto2: a URL with a scheme is the same site ask
    host, sep, path = site.partition("/")
    if (_HOST_RE.match(host.lower()) and (not sep or _PATH_RE.match(path))  # jauto2: ?&=# -> agent
            and (sep or site != arg or not _doc_name(site))  # jauto2: "report.pdf" is a file, not a host
            and len("https://" + site) <= URL_MAX):  # jauto3: same cap for both URL forms
        return Intent("open_site", site)
    url = _safe_url(arg)
    if url is not None:  # jauto3: a typed query/hash URL, already upgraded to https
        return Intent("open_site", url)
    return None


def _doc_name(name: str) -> bool:
    """jauto2: the name ends in a document extension the open policy knows (report.pdf, x.md)."""
    return "." in name and name.rpartition(".")[2].casefold() in OPEN_DOCS


def _doc_target(target: str) -> bool:
    """jauto2: "open <target>" names a document, a path or a URL rather than an app."""
    return bool(_SCHEME_RE.match(target) or "/" in target or _doc_name(target) or _DOC_TARGET_RE.match(target))


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


# Spoken lead-ins before a request ("Okay, can you show me a picture of Saturn"):
# repeated in any order, each a whole word followed by a comma and/or spaces.
_LEAD_INS = ("okay", "ok", "alright", "all right", "right", "so", "well", "um", "uh",
             "hey", "now", "please", "jarvis", "can you", "could you", "would you",
             "will you", "can we", "could we", "i want you to", "i'd like you to",
             "i need you to", "go ahead and", "just")
# Longest first so multi-word leads win; the apostrophe may be either quote.
_LEAD_RE = re.compile(r"^(?:(?:" + "|".join(
    p.replace("i'd", "i[''’]?d") for p in sorted(_LEAD_INS, key=len, reverse=True))
    + r")[\s,]+)+", re.I)
# Trailing politeness the fixed-phrase wrappers don't cover ("hide sir", "clear now").
_TAIL_RE = re.compile(r"(?:[\s,]+(?:please|for me|now|sir))+$", re.I)


def strip_lead(text: str) -> str:
    """The text minus its spoken lead-ins ("okay, can you " etc.), case kept; the input
    itself when nothing would remain ("okay" alone stays "okay")."""
    stripped = _LEAD_RE.sub("", text, count=1)
    return stripped if stripped.strip() else text


def _strip_tail(text: str) -> str:
    """The text minus trailing please/for me/now/sir; the input when nothing would remain."""
    stripped = _TAIL_RE.sub("", text)
    return stripped if stripped.strip() else text


def destutter(text: str) -> str:
    """Stuttered words off ("clear the over, overlay" -> "clear the overlay"): a word
    repeated at once ("the the") keeps its second copy only, and a word with STT
    punctuation whose letters start the next word's ("over," before "overlay") is dropped. Comparison strips
    ,.;:!? from each word's ends; kept words keep their punctuation."""
    words = text.split()
    bare = [w.strip(",.;:!?") for w in words]
    kept: list[str] = []
    for i, w in enumerate(words):
        b = bare[i]
        nxt = bare[i + 1] if i + 1 < len(words) else ""
        if b and nxt:
            if b == nxt:  # an immediate repeat: the second copy stays
                continue
            # A strict prefix of the next word, cut off by STT punctuation ("over, overlay"):
            # without the comma "the car cards" is two words, not a stutter.
            if len(b) >= 2 and nxt.startswith(b) and w != b:
                continue
        kept.append(w)
    return " ".join(kept)


def _match_forms(t: str):
    """A fixed-phrase matcher's tries: the lead-stripped text, then its tail-stripped
    form, then either of those with a stutter collapsed ("the the cat" -> "the cat")."""
    lead = strip_lead(t)
    tail = _strip_tail(lead)
    forms = [lead] if tail == lead else [lead, tail]
    seen = set(forms)
    yield from forms
    for form in forms:
        d = destutter(form)
        if d != form and d not in seen:
            seen.add(d)
            yield d


def _media_count(amt: str, plural: str, max_count: int) -> int:
    """Image count from the spoken amount: a number (1-6) capped, plural/"some" -> 4, else 1."""
    if amt.isdigit():
        return max(1, min(max_count, int(amt)))
    if amt in _MEDIA_AMOUNTS:
        return max(1, min(max_count, _MEDIA_AMOUNTS[amt]))
    if amt in ("some", "a few") or plural:
        return min(4, max_count)
    return 1


def _media_ask(kind: str, noun: str, subject: str, plural: str, amt: str) -> MediaAsk:
    """One matched media request through the shared subject rules (see media_request)."""
    from mac.voice import media_lookup  # lazy: importing intents must not pull httpx
    if not subject:
        return MediaAsk(kind, "", 0)
    count = 1 if kind == "video" else _media_count(amt, plural, media_lookup.MAX_COUNT)
    s = subject
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
    if kind == "image" and noun == "diagram" and s:
        s += " diagram"  # the cap below counts it
    s = " ".join(s.split()[:6])[:media_lookup.MAX_QUERY].strip()
    if s and not s.isdigit() and not all(w in _MEDIA_PRIVATE_WORDS for w in s.split()):
        return MediaAsk(kind, s, count, article)
    return MediaAsk(kind, "", 0)


def media_request(text: str, loose: bool = False) -> MediaAsk | None:
    """The owner's spoken picture/video request, or None. Fixed code, never a model.

    Privacy path: when the conversation holds private text, the service may not let a
    model write a search query, so the subject is built only from this transcript — it
    can never contain words the owner didn't say. subject "" (count 0) = a clear ask
    with no searchable subject (deixis, screen words).

    loose=True also picks up "<noun> of <subject>" behind words the strict fullmatch
    refuses ("so anyway, a picture of a dog"): same subject rules, never a wider net.
    """
    if len(text) > 300:
        return None
    t = _WAKE_RE.sub("", text.strip(), count=1).strip()
    t = _MISHEARD_WAKE_RE.sub("", t, count=1).strip().lower()
    sentences = [s.rstrip(",").strip() for s in re.split(r"[.?!]+", t) if s.strip()][:3]
    for sent in sentences:
        s = strip_lead(sent)
        m = _MEDIA_REQUEST_RE.fullmatch(s) or _MEDIA_OF_RE.fullmatch(s)
        if m:
            return _media_ask("video" if m["noun"] in ("video", "clip") else "image",
                              m["noun"], m["subject"], m["plural"],
                              m.groupdict().get("amt") or "")
        if loose:
            m = _MEDIA_LOOSE_RE.search(s)
            if m:
                return _media_ask("video" if m["noun"] in ("video", "clip") else "image",
                                  m["noun"], m["subject"], m["plural"], "")
    return None


def _first_sentence(target: str) -> str:
    """A card target up to its first sentence end: room words after the ask ("enlarge the phone
    diagram. Yes") are not part of the name. Only the target is cut; the parse kind is not."""
    return re.split(r"[.?!](?:\s|$)", target, maxsplit=1)[0].strip() or target


def card_size_request(text: str) -> CardSizeAsk | None:
    """Card enlarge/restore ask from the whole utterance, or None. Fixed code, never a model."""
    t = _clean(text).lower().replace(",", "")
    for form in _match_forms(t):
        m = _CARD_MAX_RE.fullmatch(form)
        if m:
            target = (m["target"] or m["make_target"]).strip()
            if target.startswith(("on ", "upon ")):
                return None
            target = _CARD_TARGET_LEAD_RE.sub("", _first_sentence(target))
            if target.endswith(" card"):
                target = target[:-5]
            return CardSizeAsk("max", target.strip())
        if _CARD_RESTORE_RE.fullmatch(form):
            return CardSizeAsk("restore", "")
    return None


def card_remove_request(text: str) -> CardRemoveAsk | None:
    """One-card removal ask ("close the phone diagram"), or None. Fixed code, never a model."""
    t = _clean(text).lower().replace(",", "")
    # Tail-stripped form first: the target is free text, so "remove that please" would
    # otherwise match with "that please" as the target.
    for form in reversed(list(_match_forms(t))):
        m = _CARD_REMOVE_RE.fullmatch(form)
        if not m:
            continue
        target = (m["t1"] or m["t2"] or "").strip()
        clear_verb = form.startswith("clear ")
        # A trailing place is politeness, not part of the name: cut BEFORE the blocked check.
        cut_place = _CARD_PLACE_RE.sub("", target).strip()
        if cut_place:
            target = cut_place
        if clear_verb and set(target.split()) & _WHOLE_SET:
            return None  # "clear every single card": many cards, the model's ask as at base
        card = "card" in target.split() or any(w in KIND_WORDS and w not in _SOFT_KIND_WORDS
                                               for w in target.split())
        if target in _CARD_REMOVE_POINTERS:
            if clear_verb:  # "clear that" never one-card-removes, whatever calls this
                return None
            return CardRemoveAsk(target, True, card)
        m_about = re.fullmatch(r"(?:the |that |this )?card (?:about|called|named|titled) (?P<x>.+)",
                               target)
        if m_about and not m_about["x"].startswith(("my ", "our ")):
            # "the card about X": X is the name. A possessive topic ("about my calendar")
            # is owner-data talk, not a card name, and keeps today's kind.
            target = m_about["x"]
            card = True
        if target.startswith(("on ", "upon ")) or set(target.split()) & _CARD_REMOVE_BLOCKED:
            return None
        cut = _first_sentence(target)
        if set(cut.split()) & _CARD_REMOVE_BLOCKED:  # "close the window. Yes" is not a card
            return None
        # Never a pointer or a bare filler from a cut: "close it. yes" must not become one.
        if cut in _CARD_REMOVE_POINTERS or not set(card_words(cut)) - _CARD_FILLERS:
            cut = target
        target = _CARD_TARGET_LEAD_RE.sub("", cut)
        if target.endswith(" card"):
            target = target[:-5]
        if clear_verb and not (set(target.split()) - _CARD_FILLERS
                               - _CARD_REMOVE_POINTERS - _CLEAR_STOP):
            return None  # "clear the overlay/screen/last card": not a one-card name
        return CardRemoveAsk(target.strip(), False, card)
    return None


def _ask_words(raw: str) -> str:
    """Spoken words for an ask: control chars off, one line, collapsed, capped."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", raw).split())[:SESSION_WORDS_MAX]


def _ask_name(raw: str) -> str:
    """A spoken session name: lower-cased, a leading the/my off."""
    return re.sub(r"^(?:the|my) ", "", raw.lower().strip()).strip()


def session_request(text: str) -> SessionAsk | None:
    """The owner's spoken session ask (start/stop/answer), or None. Fixed code, never a model."""
    if len(text) > 300:
        return None
    t = strip_lead(_clean(text))
    m = _SESSION_START_RE.fullmatch(t)
    if m:
        dev = _SESSION_DEVICES.get((m["dev"] or "mini pc").lower().replace(" ", ""), "mini_pc")
        ref = "this" if _SESSION_THIS_RE.search(m["words"]) else None
        return SessionAsk("start", dev, ref, "", _ask_words(m["words"]), None)
    m = _SESSION_STOP_RE.fullmatch(t)
    if m:
        return SessionAsk("stop", "", "name", _ask_name(m["name"] or m["name2"]), "", None)
    m = _SESSION_ANSWER_RE.fullmatch(t)
    if m:
        # "the <name> session": the group carries the trailing word; the name is <name>.
        name = _ask_name(re.sub(r"\s+session$", "", m["tname"] or m["aname"] or m["sub"]
                                or m["asub"] or "", flags=re.I))
        if m["tyn"]:
            return SessionAsk("answer", "", "name", name, "",
                              {"yes": "yes", "no": "no",
                               "to go ahead": "approve"}[m["tyn"].lower()])
        if m["aname"]:
            return SessionAsk("answer", "", "name", name, "", "approve")
        words = _ask_words(m["words"] or m["awords"] or "")
        answer = {"yes": "yes", "no": "no"}.get(words.lower(), "text")
        return SessionAsk("answer", "", "name" if (m["sub"] or m["asub"]) else "this",
                          name, words, answer)
    return None


def memory_request(text: str) -> MemoryAsk | None:
    """The owner's spoken remember/forget ask, or None. Fixed code, never a model."""
    if len(text) > 300:
        return None
    t = strip_lead(_clean(text))
    for rx in _MEMORY_ADD_RES:
        m = rx.fullmatch(t)
        if m:
            return MemoryAsk("add", _ask_words(m["fact"]))
    m = _MEMORY_RULE_RE.fullmatch(t)
    if m:
        return MemoryAsk("add", _ask_words("From now on " + m["rule"]))
    if _MEMORY_LIST_RE.fullmatch(t):
        return MemoryAsk("list", "")
    if t.lower() == "forget that":
        return MemoryAsk("forget_last", "")
    m = _MEMORY_FORGET_ABOUT_RE.fullmatch(t)
    if m:
        return MemoryAsk("forget_about", _ask_words(m["words"]))
    if _MEMORY_FORGET_ALL_RE.fullmatch(t):
        return MemoryAsk("forget_all", "")
    return None


def _ws_name(raw: str) -> str:
    """A spoken workspace name: lower-cased, a leading our/my/the off."""
    return re.sub(r"^(?:our|my|the) ", "", raw.lower().strip()).strip()


def _ws_known(name: str, names: tuple[str, ...]) -> bool:
    """Open/delete guard: a research/workspace suffix, or words inside one saved name."""
    if name.endswith(("research", "workspace")):
        return True
    words = set(name.split()) - _WORKSPACE_FILLERS
    return any(words <= set(_ws_name(n).split()) for n in names)


def workspace_request(text: str, names: tuple[str, ...] = ()) -> WorkspaceAsk | None:
    """The owner's spoken save/open/list/delete workspace ask, or None. Fixed code, never a model."""
    if len(text) > 300:
        return None
    t = strip_lead(_clean(text))
    m = _WORKSPACE_SAVE_RE.fullmatch(t)
    if m:
        return WorkspaceAsk("save", _ws_name(m["name"]))
    if _WORKSPACE_OPEN_PAGES_RE.fullmatch(t):
        return WorkspaceAsk("open_pages", "")
    m = _WORKSPACE_OPEN_RE.fullmatch(t)
    if m and _ws_known(_ws_name(m["name"]), names):
        return WorkspaceAsk("open", _ws_name(m["name"]))
    if _WORKSPACE_LIST_RE.fullmatch(t):
        return WorkspaceAsk("list", "")
    m = _WORKSPACE_DELETE_RE.fullmatch(t)
    if m and _ws_known(_ws_name(m["name"]), names):
        return WorkspaceAsk("delete", _ws_name(m["name"]))
    return None


def card_words(title: str) -> tuple[str, ...]:
    """Lower-case words of a title (apostrophes dropped, split on non-letters/digits)."""
    return tuple(_tokens(title.replace("'", "")))


# Spoken kind word -> the card types it can mean ("the picture" -> any image card).
_CARD_FLOW = frozenset({"flow", "bar"})
_CARD_CHART = frozenset({"bar", "chart"})
_CARD_DIAGRAM = frozenset({"flow", "bar", "web"})  # jfollow10c: a drawing is a web card
KIND_WORDS: dict[str, frozenset[str]] = {
    "diagram": _CARD_DIAGRAM, "diagrams": _CARD_DIAGRAM, "flow": _CARD_FLOW,
    "chart": frozenset({"flow", "bar", "chart"}), "charts": frozenset({"flow", "bar", "chart"}),
    "graph": _CARD_CHART, "graphs": _CARD_CHART, "pie": _CARD_CHART,  # jfollow10c (soft)
    "drawing": frozenset({"web"}), "drawings": frozenset({"web"}), "sketch": frozenset({"web"}),
    "picture": frozenset({"image"}), "pictures": frozenset({"image"}),
    "image": frozenset({"image"}), "images": frozenset({"image"}),
    "photo": frozenset({"image"}), "photos": frozenset({"image"}), "pic": frozenset({"image"}),
    "video": frozenset({"video"}), "videos": frozenset({"video"}),
    "list": frozenset({"bar", "info"}), "changes": frozenset({"bar", "info"}),
    # jfollow9: kinds for title matching only — these never flag a card remove by
    # themselves (_SOFT_KIND_WORDS below), so "close the notes" keeps today's parse.
    "note": frozenset({"info"}), "notes": frozenset({"info"}), "text": frozenset({"info"}),
    "info": frozenset({"info"}), "summary": frozenset({"info"}),
    "stat": frozenset({"stat"}), "stats": frozenset({"stat"}), "number": frozenset({"stat"}),
    "figure": frozenset({"stat"}),
    "link": frozenset({"link"}), "links": frozenset({"link"}),
    "clip": frozenset({"video"}), "clips": frozenset({"video"}),
    "map": frozenset({"web"}), "maps": frozenset({"web"}),  # jfollow10b (soft)
}
# The jfollow9 additions: kind words too weak to make "close the notes" a card remove.
_SOFT_KIND_WORDS = frozenset({"note", "notes", "text", "info", "summary", "stat", "stats",
                              "number", "figure", "link", "links", "clip", "clips", "map", "maps",
                              "graph", "graphs", "pie", "drawing", "drawings", "sketch"})
# Words that never name a card: pointers and politeness around the real words.
_CARD_FILLERS = frozenset({"the", "a", "an", "that", "this", "these", "those", "my", "one",
                           "card", "please", "it"})


def _fold_word(w: str) -> str:
    """A word folded to its singular-ish form for STT tolerance: trailing "es" off a
    5+ letter word ending ses/xes/ches/shes ("boxes" -> "box"), else a trailing "s" off
    a 4+ letter word not ending "ss" ("cats" -> "cat"); "glass"/"smartphone" stay."""
    if len(w) >= 5 and w.endswith(("ses", "xes", "ches", "shes")):
        return w[:-2]
    if len(w) >= 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _title_hit(t: str, w: str) -> bool:
    """A title word covers a spoken word: equal, or the spoken word (4+ letters) ends it,
    or the folded forms are equal ("notes" hears "note", "Cat's" was tokenized to "cats")."""
    return (t == w or (len(w) >= 4 and t.endswith(w))
            or _fold_word(t) == _fold_word(w))


def _ranked_title(names: list[str], kind: set[str],
                  hits: list[tuple[str, str, tuple[str, ...]]]) \
        -> list[tuple[str, str, tuple[str, ...]]]:
    """The best tier of title hits, newest-first order kept: lowest (suffix_hits,
    extra_words). suffix_hits = name words matching no title word exactly; extra_words =
    title words (minus fillers) no name covers (equals a name word, ends with a 4+ letter
    name word, or equals a spoken kind word). An exact short title beats a longer one."""
    best: list[tuple[str, str, tuple[str, ...]]] = []
    best_key: tuple[int, int] | None = None
    for c in hits:
        suffix_hits = sum(1 for w in names if w not in c[2])
        extras = sum(1 for t in c[2]
                     if t not in _CARD_FILLERS and t not in names and t not in kind
                     and not any(len(w) >= 4 and t.endswith(w) for w in names))
        key = (suffix_hits, extras)
        if best_key is None or key < best_key:
            best_key, best = key, [c]
        elif key == best_key:
            best.append(c)
    return best


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
    # A name of 4+ letters may end a compound title word ("phone" names "Smartphone block diagram").
    hits = [c for c in reversed(cards)
            if all(any(_title_hit(t, w) for t in c[2]) for w in names)]
    if kind:  # a spoken kind is a preference, not a requirement
        typed = [c for c in hits if c[1] in kind]
        if typed:
            hits = typed
    return (_ranked_title(names, kind, hits)[0][0], "title") if hits else None


def card_matches(target: str, cards: list[tuple[str, str, tuple[str, ...]]]) \
        -> tuple[str, list[str]] | None:
    """(how, ids) for every card a spoken target may mean, ids newest first; None = no card
    answers to it. match_card's logic over lists, for a removal that must show its picks."""
    by_id: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for c in cards:  # a duplicate id counts once, at its newest position and state
        by_id.pop(c[0], None)
        by_id[c[0]] = c
    cards = list(by_id.values())
    # "remove the second card": one ordinal/number word counts the cards in order
    # (first = the oldest here); out of range means no card answers.
    pwords = [w for w in card_words(target)
              if w not in _CARD_FILLERS and w not in ("card", "number")]
    if len(pwords) == 1 and pwords[0] in _POSITIONS:
        i = _POSITIONS[pwords[0]]
        i = i if i >= 0 else len(cards) + i
        return ("position", [cards[i][0]]) if 0 <= i < len(cards) else None
    words = [w for w in card_words(target)
             if w not in _CARD_FILLERS and w not in ("last", "newest")]
    kind: set[str] = set()
    for w in words:
        kind |= KIND_WORDS.get(w, frozenset())
    names = [w for w in words if w not in KIND_WORDS]
    if not names:
        if not kind:
            return ("newest", [cards[-1][0]]) if cards else None
        typed = [c for c in reversed(cards) if c[1] in kind]
        titled = [c for c in typed if any(t in words for t in c[2])]
        if len(titled) == 1:
            return ("title", [titled[0][0]])
        if len(typed) == 1:
            return ("kind", [typed[0][0]])
        if typed:
            return ("kind", [c[0] for c in (titled if titled else typed)])
        return None
    # A name of 4+ letters may end a compound title word ("phone" names "Smartphone block
    # diagram"); a spoken kind is a preference, not a requirement.
    hits = [c for c in reversed(cards)
            if all(any(_title_hit(t, w) for t in c[2]) for w in names)]
    if kind:
        typed = [c for c in hits if c[1] in kind]
        if typed:
            hits = typed
    return ("title", [c[0] for c in _ranked_title(names, kind, hits)]) if hits else None


# --- jfollow6: pure helpers for the overlay service's pick/claim/name flows (part B/C) ---

_PICK_TAIL_RE = re.compile(r"(?:[\s,]+(?:please|sir|thanks|jarvis))+$")
_PICK_VERB_RE = re.compile(r"^(?:remove|close|dismiss|clear|enlarge|maximi[sz]e|expand"
                           r"|get rid of) ")
_PICK_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": -1}
_PICK_NUMBERS = {"one": 0, "two": 1, "three": 2}
# One-card removal by position: the spoken word -> the index into the cards in order.
_POSITIONS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": -1,
              "one": 0, "two": 1, "three": 2, "four": 3, "five": 4,
              "1": 0, "2": 1, "3": 2, "4": 3, "5": 4}
_PICK_ALL = frozenset({"both", "both of them", "all", "all of them", "all three",
                       "all of those"})


def _pick_text(text: str) -> str:
    """A pick reply normalized: cleaned, lower case, commas/lead-ins/tails/verb off."""
    t = strip_lead(_clean(text).lower().replace(",", ""))
    t = _PICK_TAIL_RE.sub("", t).strip() or t
    return _PICK_VERB_RE.sub("", t, count=1).strip()


def pick_shaped(text: str) -> bool:
    """True when the reply is shaped like a pick answer ("the second one", "both")."""
    t = _pick_text(text)
    return t in _PICK_ALL or t.rsplit(" ", 1)[-1] in {
        "one", "card", "cards", "first", "second", "third", "fourth", "last"}


def card_pick(text: str, names: list[tuple[str, ...]]) -> list[int] | None:
    """Indexes into `names` a "Which one" reply picks, or None. Ordinals and both/all
    first; then a name reference, kept only when the ranking leaves exactly one."""
    t = _pick_text(text)
    if t in _PICK_ALL:
        return list(range(len(names)))
    words = t.split()
    if words and words[0] == "the":
        words = words[1:]
    if words and words[-1] in ("one", "card"):
        words = words[:-1]
    if len(words) == 2 and words[0] == "number":
        i = _PICK_NUMBERS.get(words[1], len(names))
        return [i] if 0 <= i < len(names) else None
    if len(words) == 1 and words[0] in _PICK_ORDINALS:
        i = _PICK_ORDINALS[words[0]]
        i = i if i >= 0 else len(names) + i
        return [i] if 0 <= i < len(names) else None
    if not words:
        return None
    for ns in (names, [tuple(w for w in n if w != "jarvis") for n in names]):
        # second pass: the names as the voice said them (it never speaks the wake word)
        m = card_matches(t, [(str(i), "", n) for i, n in enumerate(ns)])
        if m and m[0] == "title" and len(m[1]) == 1:
            return [int(m[1][0])]
    return None


# jfollow9: the ways the companion itself is spoken of ("what can the Jarvis company do").
_COMPANION_ALIAS_RE = re.compile(
    r"\b(?:jarvis companions?|java'?s companions?|jarvis company|companion app"
    r"|the companion)\b")
# Round 5 (owner rows 21:00:31, 21:02:24): naming the Companion is not asking about it. Only a
# question about what it is, does, can do, covers or includes counts, or the bare name as a reply.
_COMPANION_ASK_RE = re.compile(
    r"\b(?:what(?:'s| is| are| does| do| can| could| else)|how does|how do|tell me (?:more )?about"
    r"|explain|describe|capabilit\w*|features?|abilities|cover(?:s|ed)?|includes?|consists? of)\b")
# A task naming it ("start a session to help the Jarvis companion app ...") goes on as the same
# sentence without the name: a task verb opening the sentence or a clause.
_COMPANION_TASK_RE = re.compile(
    r"(?:^|[.,;!?]\s*|\b(?:and|then|please|to|so)\s+)(?:start|sort|create|open|fix|build|change|add"
    r"|remember|make|help|allow|let|enable|set up|implement|install|update|write|edit|delete|remove)\b"
    r"|\b(?:repo|repository|codebase|commits?)\b")  # "check the repository" is work on it
_COMPANION_NAME_ONLY_RE = re.compile(
    r"(?:(?:i meant|i mean|no|yes|okay|ok) )?(?:the )?(?:jarvis |java'?s )?(?:companions?|company)(?: app)?")


def about_companion(text: str) -> bool:
    """True when the transcript asks about Jarvis Companion itself (any spoken alias): what it
    is, does, can do, covers or includes, or the bare name. A task that only names it, a card
    ask, a pick reply or a sessions ask: False."""
    raw = text.strip().lower().replace("’", "'")
    low = _clean(text).lower().replace("’", "'")
    # The wake-word strip eats a leading "Jarvis " ("Jarvis companions features"), so the
    # aliases are searched on the raw text as well as the cleaned one.
    if not (_COMPANION_ALIAS_RE.search(raw) or _COMPANION_ALIAS_RE.search(low)):
        bare = re.sub(r"^(?:hey |jarvis )+", "",
                      re.sub(r"[^a-z ]", " ", low)).strip()
        if bare not in ("companion", "companions"):
            return False
    if _form_hit(_SESSIONS_RE, low.replace(",", "")) or _form_hit(_SESSIONS_TOPIC_RE,
                                                                  low.replace(",", "")):
        return False
    words = raw.split()
    if "card" in words or "cards" in words or card_remove_request(text) is not None:
        return False
    if pick_shaped(text):
        return False
    if any(_COMPANION_NAME_ONLY_RE.fullmatch(" ".join(re.sub(r"[^a-z' ]", " ", f).split()))
           for f in (raw, low)):
        return True
    return bool(_COMPANION_ASK_RE.search(raw)) and not (
        _COMPANION_TASK_RE.search(raw) or _COMPANION_TASK_RE.search(strip_lead(raw)))


_CLAIM_PAST = r"cleared|removed|closed|hid|hidden|dismissed|enlarged|shrank|shrunk|taken away|got rid"
_CLAIM_BASE = r"clear|remove|close|hide|dismiss|enlarge|shrink|take away|get rid of"
_CLAIM_ING = r"clearing|removing|closing|hiding|dismissing|enlarging|shrinking|taking away|getting rid"
_CLAIM_GONE = r"gone|removed|cleared|closed|hidden|dismissed"
_CLAIM_RE = re.compile(
    r"\b(?:i've|i have|i)\s+(?P<v1>" + _CLAIM_PAST + r")"
    r"|\b(?:i'll|i will|(?:i'm|i am) going to)\s+(?P<v2>" + _CLAIM_BASE + r")"
    r"|\b(?:i'm|i am)\s+(?P<v3>" + _CLAIM_ING + r")"
    r"|(?:^|,\s*)(?P<v4>" + _CLAIM_ING + r")"
    r"|\b(?P<v5>is|are|has been|have been)\s+(?:" + _CLAIM_GONE + r")"
    r"|\bdone\b[^.!?]*\b(?P<v6>is|are|has been|have been)\s+(?:" + _CLAIM_GONE + r")",
    re.I)  # "I've" arrives capitalized
_CLAIM_SUBJECT_RE = re.compile(r"\b(?:card|cards|overlay)\b", re.I)
_CLAIM_NEG_RE = re.compile(r"\b(?:can't|cannot|won't|don't|didn't|not)\s+$", re.I)
# "Two cards overlapped. I'll clear them." (Mac live run): a pronoun carries the card word over.
_CLAIM_PRONOUN_RE = re.compile(r"\b(?:it|them|they|those|these|both)\b", re.I)


def claims_overlay_change(line: str) -> bool:
    """True when a model line claims an overlay change ("Clearing the cards"): per
    sentence, a card/overlay word (or a pronoun when another sentence has one) plus a
    claim form; a negator before the verb isn't."""
    line = line.replace("’", "'")
    named = bool(_CLAIM_SUBJECT_RE.search(line))
    for sent in re.split(r"[.!?]", line):
        sent = sent.strip()  # a later sentence's "Clearing ..." starts at ^ too
        if not (_CLAIM_SUBJECT_RE.search(sent) or named and _CLAIM_PRONOUN_RE.search(sent)):
            continue
        for m in _CLAIM_RE.finditer(sent):
            verb_at = next(m.start(g) for g in ("v1", "v2", "v3", "v4", "v5", "v6")
                           if m.group(g) is not None)
            if not _CLAIM_NEG_RE.search(sent[:verb_at]):
                return True
    return False


_NOTE_MINE = r"(?:not|wasn't|was not|isn't|is not) from you"
_NOTE_SOURCE_RE = re.compile(
    r"\b(?:search results?|web results?|results|" + _NOTE_MINE + r")\b"
    r"|\b(?:the|a|one|that|those|these)\s+(?:\w+\s+)?"
    r"(?:pages?|sites?|websites?|sources?|articles?|snippets?|results?)\b",
    re.I)
_NOTE_INSTRUCTION_RE = re.compile(
    r"\binstructions?\b|\bhidden\s+(?:line|text|note)\b|\ba line\s+(?:telling|asking)\b", re.I)
_NOTE_DIRECTIVE_RE = re.compile(
    r"\b(?:told|asked|telling|asking|wanted|tried to get|instructing)\s+me\s+to\b"
    r"|\btried to (?:tell|make) me\b", re.I)
_NOTE_IGNORE_RE = re.compile(
    r"\b(?:ignored|ignore it|ignoring|disregard\w*|skipped|didn't follow|did not follow|won't follow"
    r"|not going to follow|didn't do|did not do)\b", re.I)
_NOTE_MINE_RE = re.compile(r"\b" + _NOTE_MINE + r"\b", re.I)
# R5 (Mac run): "...ended with an instruction to add source links to my reply." with its "I ignored
# it" in another clause or say line. Only that narrow shape, and only when a note was already
# dropped in the same reply (R6 security): alone it is spoken (loud and safe). Never on a clause
# with "I", a question or a warning, so an admission, a question or a warning to the owner stays.
# R6: the clause must END at "my reply" (", so they are included below" is an obey tail), and
# a next clause in the same text that speaks as "I" without ignoring ("I added them.") keeps it.
_NOTE_AIMED_RE = re.compile(
    r"\binstructions?\s+to\s+(?:add|include|put|insert|append|attach)\b[^.!?;:]{0,60}?"
    r"\b(?:to|in|into)\s+my\s+(?:reply|replies|answer|answers|response|responses)\s*[.!]?\s*$", re.I)
_NOTE_AIMED_VETO_RE = re.compile(r"\bi\b|\?|\b(?:warn\w*|unsafe|careful|caution|beware|trust)\b", re.I)
_NOTE_REAL_SOURCE_RE = re.compile(_NOTE_SOURCE_RE.pattern.replace("|" + _NOTE_MINE, ""), re.I)
# R2: "..., and I emailed him." admits obeying a web result: never hidden. Any first-person
# verb counts unless it is an ignore/refusal word ("which I have ignored" is still a note).
_NOTE_IGN = (r"(?:ignored|ignore|skipped|skip|disregarded|disregard|left|leave|dropped|refused|declined"
             r"|chose (?:not|to ignore)|decided (?:not|to ignore)|didn't|did not|won't|will not"
             r"|wouldn't|would not|never|not)")
_NOTE_OBEY_RE = re.compile(
    r"\b(?:and|so|then|which) i(?:'ve| have)? (?!(?:\w+ly |just |also |then |have |had )?"
    + _NOTE_IGN + r"\b)\w+", re.I)
# R1: a bare "I ignored it." after a dropped note is part of that note.
_NOTE_BARE_IGNORE_RE = re.compile(
    r"(?:and |so |which |but )?i (?:ignored|skipped|disregarded|didn't follow|did not follow"
    r"|won't follow|didn't do|did not do)(?: (?:it|that|them|this|those|the instruction"
    r"|that instruction|the request))?[.!?]?", re.I)


def drop_injection_notes(text: str, noted: bool = False) -> tuple[str, int]:
    """Removes model sentences (and ";" clauses) that narrate an instruction injected by a web
    result ("...told me to add source links... I ignored it"); it only ever removes text,
    never adds. A refusal or answer sharing the sentence stays; an admission of obeying stays.
    `noted`: a note was already dropped earlier in this reply (only widens `_NOTE_AIMED_RE`)."""
    split = lambda x: [re.split(r"\s*;\s*", s) for s in re.split(r"(?<=[.!?])\s+", x.strip())]
    sents = split(text)
    tsents = split(text.replace("’", "'"))  # matching only: the kept text stays as spoken
    units = [(si, ci, c) for si, cl in enumerate(tsents) for ci, c in enumerate(cl)]
    flags = [(_NOTE_INSTRUCTION_RE.search(c), _NOTE_IGNORE_RE.search(c), _NOTE_OBEY_RE.search(c))
             for _, _, c in units]
    drop: set[tuple[int, int]] = set()
    dropped = 0
    convicting = sum(1 for f in flags if f[0] and f[1])  # clauses naming + ignoring an instruction
    for k, (si, ci, c) in enumerate(units):
        if (si, ci) in drop or not _NOTE_SOURCE_RE.search(c) or flags[k][2]:
            continue
        instr, ignore, _ = flags[k]
        directive = _NOTE_DIRECTIVE_RE.search(c)
        mine = _NOTE_MINE_RE.search(c)
        bare_next = k + 1 < len(units) and _NOTE_BARE_IGNORE_RE.fullmatch(units[k + 1][2])
        # (b): the directive clause may lack its own "I ignored it": another clause naming the
        # instruction and ignoring it convicts this one.
        other = convicting - bool(instr and ignore) > 0
        nxt = units[k + 1][2] if k + 1 < len(units) else ""
        aimed = (instr and (noted or dropped or other) and _NOTE_AIMED_RE.search(c)
                 and not _NOTE_AIMED_VETO_RE.search(c)
                 and not (re.search(r"\bi\b", nxt, re.I) and not _NOTE_IGNORE_RE.search(nxt)))
        if ((instr and (ignore or mine or directive or bare_next)) or aimed
                or (directive and (ignore or other or bare_next))
                or (mine and ignore and _NOTE_REAL_SOURCE_RE.search(c))):
            dropped += 1
            drop.add((si, ci))
            if bare_next:
                drop.add(units[k + 1][:2])
    if not dropped:
        return text, 0
    out = []
    for si, cl in enumerate(sents):
        kept = [c for ci, c in enumerate(cl) if (si, ci) not in drop]
        if not kept:
            continue
        line = "; ".join(kept)
        if (si, len(cl) - 1) in drop:  # the sentence end was dropped: keep its stop
            line = line.rstrip(" ,") + (cl[-1][-1] if cl[-1][-1:] in ".!?" else ".")
        out.append(line)
    return " ".join(out), dropped


# "close the overlay" names nothing: these never carry a one-card name ("up"/"down"/"off"/
# "out" are verb particles: "close down the overlay"; "the whole overlay" is no name either).
_CARD_NAME_STOP = frozenset({"overlay", "screen", "these", "those", "them", "cards",
                             "up", "down", "off", "out"}) | _WHOLE_SET


def names_one_card(text: str) -> bool:
    """True when an ask names exactly one card to act on (Design C3)."""
    ask = first_form(text, card_remove_request)
    if ask and not ask.pointer:
        left = [w for w in card_words(ask.target)
                if w not in _CARD_FILLERS and w not in KIND_WORDS and w not in _CARD_NAME_STOP]
        if left:
            return True
    toks = card_words(_clean(text).lower())
    return "card" in toks and not any(
        w in toks for w in ("cards", "everything", "all", "every", "each", "both"))


def _answer(text: str) -> str:
    """Whole-utterance form for yes/no/cancel: cleaned, lower case, punctuation as spaces."""
    t = re.sub(r"[,;:.!?]+", " ", _clean(text).casefold().replace("’", "'"))
    return " ".join(t.split())


def is_confirm(text: str) -> bool:
    """Whole utterance is a yes to the agent's confirmation question."""
    return bool(_CONFIRM_RE.fullmatch(_answer(text)))


def is_cancel(text: str) -> bool:
    """Whole utterance cancels the agent's work."""
    return any(_CANCEL_RE.fullmatch(f) for f in _match_forms(_answer(text)))


def is_stop(text: str) -> bool:
    """Whole utterance stops the turn: silence, a cancel, or both."""
    return any(_STOP_RE.fullmatch(f) for f in _match_forms(_answer(text)))


# Round 5 (owner row 21:02:40: "okay Jarvis stop" came out "Stop. Driver stop" and was dropped as a
# short follow-up): a stop word with only filler or address words around it and at most one unknown
# word (the mis-heard name) is a stop. A negation ("don't stop") never is.
_STOP_WORDS = frozenset({"stop", "cancel", "abort"})
_STOP_FILLERS = frozenset({"okay", "ok", "hey", "please", "now", "no", "jarvis", "sir", "it", "that",
                           "just", "right", "enough"})
_STOP_NEGATIONS = frozenset({"don't", "dont", "not", "never", "can't", "won't"})
STOP_FAMILY_MAX_WORDS = 5


def is_stop_family(text: str) -> bool:
    """A short stop phrase with fillers and at most one unknown word ("Stop. Driver stop")."""
    words = _answer(text).split()
    if (not words or len(words) > STOP_FAMILY_MAX_WORDS or not _STOP_WORDS.intersection(words)
            or _STOP_NEGATIONS.intersection(words)):
        return False
    other = [i for i, w in enumerate(words) if w not in _STOP_WORDS and w not in _STOP_FILLERS]
    last_stop = max(i for i, w in enumerate(words) if w in _STOP_WORDS)
    # The one unknown word is the mis-heard name before the stop word, never its object ("stop music").
    return not other or (len(other) == 1 and other[0] < last_stop)


def is_no(text: str) -> bool:
    """Whole utterance says no to the agent's confirmation question."""
    return any(_NO_RE.fullmatch(f) for f in _match_forms(_answer(text)))


def is_hunk_back(text: str) -> bool:
    """Whole utterance is a hunk-walk "go back" (applied only with a change list)."""
    return any(HUNK_BACK_RE.fullmatch(f) for f in _match_forms(_clean(text).lower()))


def tail_hunk(text: str) -> str | None:
    """\"next\"/\"prev\" when the transcript ENDS with a whole hunk phrase, else None.

    The phrase may trail a dictation sentence ("... Next change"); a hunk phrase
    buried in a longer sentence ("the next change in the law") is not a command.
    """
    sentences = re.split(r"[.!?]\s+", _clean(text).lower())
    s = sentences[-1].strip().rstrip(".?!").strip()
    for form in _match_forms(s):
        words = form.split()
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


def link_label(url: str) -> str:
    """What the overlay says for a site open: the URL when short and plain, else just the host."""
    if "?" not in url and "#" not in url and len(url) <= 60:
        return url
    return f"the link on {urllib.parse.urlsplit(url).netloc}"  # jauto3: query URLs stay private


def site_row_arg(arg: str) -> str:
    """What the debug row keeps: the arg as-is when it is said aloud, else only the host."""
    url = arg if _SCHEME_RE.match(arg) else "https://" + arg  # jauto3
    return arg if link_label(url) == url else urllib.parse.urlsplit(url).netloc


def open_site(target: str, *, run=subprocess.run) -> str | None:
    """Open an https URL built from a validated host + safe path; None = refused."""
    target = target.strip()
    if _SCHEME_RE.match(target):  # jauto3: the full https form (parse upgrades http) re-validates whole
        url = _safe_url(target) if target[:8].lower() == "https://" else None
        if url is None:
            return None
    else:
        host, sep, path = target.partition("/")
        if not _HOST_RE.match(host.lower()):
            return None
        if sep and not _PATH_RE.match(path):
            return None
        if len("https://" + target) > URL_MAX:  # jauto3
            return None
        url = "https://" + target
    run(["open", url], check=False)
    return link_label(url)


# jfollow7 S2: the follow-up rule's words (only after a sessions answer, one turn, 30 s): the
# plural "sessions", "the/my/our/that/those/these session(s)", or a follow-up device after
# on/in/through/via (mini PC + STT forms, router, companion; never "mac": "a screenshot on
# the Mac" is no sessions question). A start/stop/open verb never counts.
_FOLLOW_SESSIONS_RE = re.compile(
    r"\bsessions\b|\b(?:the|my|our|that|those|these) sessions?\b"
    r"|\b(?:on|in|through|via) (?:(?:my|the|our) )?(?:(?:mini|many) ?(?:p ?c|b ?c|piece)|minipc|router"
    r"|(?:\w+(?:'s|s')? )?companion)\b")
_FOLLOW_VERB_RE = re.compile(r"\b(?:start|stop|end|kill|close|cancel|spin up|open|restart|resume|launch|begin|create)\b")


def sessions_followup(text: str) -> bool:
    """True when a reply right after a sessions answer is still about sessions (pure, no state)."""
    low = _clean(text).lower().replace(",", "")
    return ((bool(_FOLLOW_SESSIONS_RE.search(low)) or sessions_anaphor(text))
            and not _FOLLOW_VERB_RE.search(low))


# jfollow10g: the owner's filter for a sessions list, from his own words only (never a model slot,
# row or screen text). "router", "companion" and "app" never filter: router sessions = every
# session on the Jarvis Companion app.
@dataclass(frozen=True)
class SessionsFilter:
    done: bool = False  # the app's DONE column (phase == "done")
    device: str = ""  # "" | "mac" | "minipc" | "windows"
    when: str = ""  # "" | "today" | "yesterday" | "week" (rolling 7 days by last activity)


_FILTER_DONE_RE = re.compile(r"\b(?:done|finished|completed|complete|ended)\b")
_FILTER_DEVICES = (
    ("minipc", re.compile(r"\b(?:mini|many) ?(?:p ?c|b ?c|piece)\b|\bminipc\b")),
    ("windows", re.compile(r"\bwindows\b")),
    ("mac", re.compile(r"\b(?:on|in|from) (?:(?:my|the) )?mac\b|\bmac sessions?\b")),
)
_FILTER_WHEN = (
    ("today", re.compile(r"\btoday\b")),
    ("yesterday", re.compile(r"\byesterday\b")),
    ("week", re.compile(r"\b(?:this|past) week\b|\b(?:last|past) (?:7|seven) days\b")),
)
# A follow-up pointing back at the list just given; it counts only with a filter word beside it.
_FOLLOW_ANAPHOR_RE = re.compile(r"\b(?:the ones|those ones|which ones|which of them|those)\b")


def sessions_filter(text: str) -> SessionsFilter:
    """The owner's words -> the list filter (pure). Only the first device and time word count."""
    low = _clean(text).lower().replace(",", "")
    device = next((name for name, rx in _FILTER_DEVICES if rx.search(low)), "")
    when = next((name for name, rx in _FILTER_WHEN if rx.search(low)), "")
    return SessionsFilter(bool(_FILTER_DONE_RE.search(low)), device, when)


def sessions_anaphor(text: str) -> bool:
    """ "the ones that ran this week", "which ones were on the Mac": a pointer back plus a filter word.
    A filter word alone ("what's the weather today") never makes a sessions follow-up."""
    low = _clean(text).lower().replace(",", "")
    return bool(_FOLLOW_ANAPHOR_RE.search(low)) and sessions_filter(text) != SessionsFilter()



# --- jfollow10b: show, don't link; maps, roads, TfL, saved places, YouTube questions ------------------

_LINK_WORDS_RE = re.compile(r"\b(?:links?|url|web ?site|webpage|web page|open it|open that|in the browser)\b", re.I)


def wants_link(text: str) -> bool:
    """The owner's own words ask for a link, a URL or a website, or to open it."""
    return bool(_LINK_WORDS_RE.search(text or ""))


@dataclass
class GeoAsk:
    kind: str       # geo_map | geo_roads | transit_next | transit_status | transit_journey | place_save | place_forget
    a: str = ""     # from-place words ("" = home), or the place text to save
    b: str = ""     # to-place words, or the saved name
    mode: str = ""  # transit_next: bus | tube; transit_status: line id or "tube"


_SHOW = r"(?:(?:show me|give me|pull up|get me|bring up|show) )?(?:a |the )?"
_GEO_MAP_RES = (
    re.compile(_SHOW + r"(?:map|route|directions)(?: from (?P<a>.+?))? to (?P<b>.+)"),
    re.compile(_SHOW + r"map of (?P<a>.+)"),
    re.compile(r"how do i get(?: from (?P<a>.+?))? to (?!(?:grips|know|sleep|be|terms|the bottom|the point)\b)(?P<b>.+)"),
)
_GEO_ROADS_RE = re.compile(r"(?:(?:what|which)(?: are the)? (?:roads|routes)(?: (?:could|can|should) i take)?"
                           r"|what are the roads i (?:could|can|should) take)(?: to (?P<b>.+))?")
_TRANSIT_NEXT_RES = (
    re.compile(r"(?:when(?:'s| is| does)|what(?:'s| is| time is)) the next (?P<mode>bus|tube|train|underground)"
               r"(?: (?:come|arrive|leave|go))?(?: (?:from|near|at) (?P<a>.+))?"),
    re.compile(r"(?:the )?next (?P<mode>bus|tube|train)(?: times)?(?: (?:from|near|at) (?P<a>.+))?"),
)
_TRANSIT_STATUS_RES = (
    re.compile(r"is the (?P<line>[a-z& ]{2,30}?) line (?:running|working|ok|okay|on time|delayed|late|suspended)"),
    re.compile(r"(?:are there )?(?:any )?(?:delays|problems) on the (?P<line>[a-z& ]{2,30}?) line"),
    re.compile(r"how(?:'s| is) the (?P<line>[a-z& ]{2,30}?) line(?: doing| running)?"),
    re.compile(r"is the (?:tube|underground) (?:running|working|ok|okay)"),
    re.compile(r"(?:are there )?any (?:delays|problems) on the (?:tube|underground)"),
    re.compile(r"(?:what's the |what is the )?tube status"),
)
_TRANSIT_JOURNEY_RES = (
    re.compile(r"which bus (?:do i|should i|can i) (?:take|get|catch)(?: from (?P<a>.+?))? to (?P<b>.+)"),
    re.compile(r"(?:which|what) bus(?: goes)? to (?P<b>.+)"),
    re.compile(r"is there a bus to (?P<b>.+)"),
    re.compile(r"how do i get(?: from (?P<a>.+?))? to (?P<b>.+?) by (?:bus|tube|public transport|train)"),
)
_PLACE_SAVE_RE = re.compile(r"save (?P<a>.+?) as (?:my |the )?(?P<n>[a-z0-9' ]{1,24})")
_PLACE_FORGET_RE = re.compile(r"forget (?:my|the) (?P<n>[a-z0-9' ]{1,24}?)(?: (?P<w>place|address|location))?")
_PLACE_SAVE_BLOCKED = frozenset({"this", "these", "them", "everything", "the cards", "the pages", "it", "that"})
# "save the document as report" is a file save, never a place: these A words never name a place.
_PLACE_SAVE_NOT_PLACE_RE = re.compile(
    r"(?:that|it|this|these|them|everything|my work)(?: .*)?"
    r"|(?:the|my|a|an) (?:file|document|doc|page|pages|note|notes|sheet|spreadsheet|project|work|draft|card|cards"
    r"|tab|tabs|window|code|changes|image|picture|photo|screenshot)s?(?: .*)?")
_HOME_WORDS = frozenset({"house", "home", "place", "flat", "my house", "my home", "my place", "my flat"})
# Fixed TfL tube line ids: "the purple line" names none, so no call is made.
TUBE_LINES = {
    "bakerloo": "bakerloo", "central": "central", "circle": "circle", "district": "district",
    "hammersmith and city": "hammersmith-city", "hammersmith & city": "hammersmith-city",
    "jubilee": "jubilee", "metropolitan": "metropolitan", "northern": "northern",
    "piccadilly": "piccadilly", "victoria": "victoria", "waterloo and city": "waterloo-city",
    "waterloo & city": "waterloo-city", "elizabeth": "elizabeth",
}


def place_name(words: str) -> str:
    """Spoken place words -> a saved-place key: "my" or "the" off; house, home, my place, flat -> home."""
    w = " ".join((words or "").lower().replace("’", "'").split()).strip(" .,?!")
    if w in _HOME_WORDS:
        return "home"
    w = re.sub(r"^(?:my|the|our) ", "", w)
    return "home" if w in _HOME_WORDS else w


def geo_request(text: str, place_names: tuple[str, ...] = ()) -> GeoAsk | None:
    """A whole-utterance map / roads / TfL / saved-place ask, or None. Fixed code, owner words only."""
    if len(text) > 300:
        return None
    low = _clean(text).lower().replace(",", "")
    m = _form_hit(_PLACE_SAVE_RE, low)
    if m and m["a"].strip() not in _PLACE_SAVE_BLOCKED and not _PLACE_SAVE_NOT_PLACE_RE.fullmatch(m["a"].strip()):
        return GeoAsk("place_save", m["a"].strip(), place_name(m["n"]))
    m = _form_hit(_PLACE_FORGET_RE, low)
    if m and (m["w"] or place_name(m["n"]) in place_names):
        return GeoAsk("place_forget", "", place_name(m["n"]))
    for rx in _TRANSIT_JOURNEY_RES:  # before map: "how do I get to X by bus" is a journey
        m = _form_hit(rx, low)
        if m:
            return GeoAsk("transit_journey", (m.groupdict().get("a") or "").strip(), m["b"].strip())
    for rx in _TRANSIT_STATUS_RES:
        m = _form_hit(rx, low)
        if m:
            line = (m.groupdict().get("line") or "").strip()
            return GeoAsk("transit_status", "", "", TUBE_LINES.get(line, "?") if line else "tube")
    for rx in _TRANSIT_NEXT_RES:
        m = _form_hit(rx, low)
        if m:
            return GeoAsk("transit_next", (m["a"] or "").strip(), "", "bus" if m["mode"] == "bus" else "tube")
    m = _form_hit(_GEO_ROADS_RE, low)
    if m:
        return GeoAsk("geo_roads", "", (m["b"] or "").strip())
    for rx in _GEO_MAP_RES:
        m = _form_hit(rx, low)
        if m:
            g = m.groupdict()
            return GeoAsk("geo_map", (g.get("a") or "").strip(), (g.get("b") or "").strip())
    return None


# --- jfollow10f: general places ("best way to the cinema", "nearest Dishoom", "is Ebenezer on near me") ---

CATEGORY = {"pharmacy": "pharmacy", "chemist": "pharmacy", "supermarket": "supermarket", "cafe": "cafe",
            "coffee shop": "cafe", "restaurant": "restaurant", "pub": "pub", "bank": "bank", "cash machine": "atm",
            "cash point": "atm", "cashpoint": "atm",
            "atm": "atm", "post office": "post office", "hospital": "hospital", "petrol station": "petrol station",
            "library": "library", "park": "park", "gym": "gym", "bakery": "bakery", "bookshop": "bookshop"}
_CINEMA_WORDS = frozenset({"cinema", "cinemas", "pictures", "movies", "movie theatre", "movie theater"})
_NOT_A_PLACE = r"(?!(?:grips|know|sleep|be|terms|the bottom|the point|work|school|it|that)\b)"
_PLACE_BEST_RES = (
    re.compile(r"(?:what'?s |what is )?(?:the )?best way to (?:go to|get to) " + _NOT_A_PLACE + r"(?P<x>.+)"),
    re.compile(r"(?:what'?s |what is )?(?:the )?best way to (?:go|get) (?P<x>home)"),
    re.compile(r"(?:what'?s |what is )?(?:the )?best way to (?P<x>(?:the|a|an) .+)"),
    re.compile(r"(?:what'?s |what is )?(?:the )?best way to (?P<bare>.+)"),
    re.compile(r"how (?:do|can|should) i get to (?:the )?(?:nearest|closest) (?P<x>.+)"),
    re.compile(r"(?:what'?s |what is |where'?s |where is |find(?: me)? )?(?:the |a )?(?:nearest|closest) (?P<x>.+)"),
)
_PLACE_FIND_RE = re.compile(r"find(?: me)? (?:a|an) (?P<x>.+)", re.I)  # case kept: a name is capitalised
_FIND_MEDIA_RE = re.compile(r"\b(?:picture|photo|image|video|youtube|song|clip|gif|map|recipe|film|movie|book)s?\b", re.I)
_FILM_RE = re.compile(r"is (?P<film>.+?) (?P<verb>on|showing|playing)"
                      r"(?P<place> near me| nearby| at the cinema| in cinemas| at the pictures)?"
                      r"(?P<when> tonight| today| tomorrow)?")
_FILM_STOP = frozenset({"tv", "telly", "light", "lights", "heating", "oven", "wifi", "bluetooth", "camera", "mic",
                        "screen", "overlay", "alarm", "music", "it", "that", "this", "they", "he", "she", "there"})
_WHATS_ON_RES = (
    re.compile(r"what'?s on at the (?:cinema|pictures|movies)(?P<when> tonight| today| tomorrow)?"),
    re.compile(r"what is on at the (?:cinema|pictures|movies)(?P<when> tonight| today| tomorrow)?"),
    re.compile(r"what films are on(?P<when> tonight| today)?(?: near me)?"),
)
_PLACE_WORDS_RE = re.compile(r"^[a-z0-9 '&-]{1,60}$")
_PLACE_POSTCODE_RE = re.compile(r"^[a-z]{1,2}\d[a-z\d]? ?\d[a-z]{2}$")  # = geo.POSTCODE_RE (intents stays light)
PLACE_CUE_RE = re.compile(r"\b(?:best way to|nearest|closest|take me)\b", re.I)  # the service loads saved names then
# jfollow11f: "take me to Dishoom" / "take me home"; "take me back", "take me through ..." never match.
_TAKE_ME_RE = re.compile(r"take me (?:to (?P<x>.+)|(?P<home>home))")
_TAKE_ME_NOT_RE = re.compile(
    r"^(?:the )?(?:top|bottom|end|start|beginning|next|previous|last|first)\b|^(?:it|that|this|there)$"
    r"|\b(?:pages?|tabs?|settings|websites?|sites?|links?|lines?|changes?|chapters?|slides?|steps?)\b"
    # round 1: screen, dev and day words are never a place ("take me to the repo", "take me to bed")
    r"|\b(?:dashboards?|files?|folders?|repos?|repository|videos?|windows?|workspaces?|overlay|cards?|maps?"
    r"|code|inbox|notes|main|minutes?|seconds?|youtube|github|bed|lunch|dinner|breakfast)\b")


def _place_target(x: str, place_names: tuple[str, ...]):
    """Target words -> ("geo", words) for a saved name / home / postcode, ("place", mode, query), or None."""
    x = " ".join(x.replace("’", "'").split()).strip(" .,?!")
    # "best way to get to the nearest Dishoom": the target is "dishoom" (a saved name then still wins)
    x = re.sub(r"^(?:(?:the|a|an) )?(?:nearest|closest) ", "", x)
    if not x or "://" in x or "www." in x or len(x) > 60:
        return None
    if x.startswith("my ") or place_name(x) == "home" or place_name(x) in place_names or _PLACE_POSTCODE_RE.fullmatch(x):
        return ("geo", x)
    bare = re.sub(r"^(?:the|a|an) ", "", x)
    if not _PLACE_WORDS_RE.fullmatch(bare) or bare in ("nearest", "closest"):
        return None
    if bare in _CINEMA_WORDS:
        return ("place", "cinema", "cinema")
    if bare in CATEGORY:
        return ("place", "category", CATEGORY[bare])
    return ("place", "name", bare)


def _take_me_target(x: str, apps: dict[str, str]) -> bool:
    """A "take me to <x>" target that may be a place: not a page spot, a screen thing, an app or a site."""
    bare = re.sub(r"^(?:the )?(?:(?:nearest|closest) )?", "", x).strip()
    return not (_TAKE_ME_NOT_RE.search(x) or re.search(r"\b(?:to|from|than)\b", x) or "://" in x
                or "www." in x or _HOST_RE.match(bare) or (apps and _match_app(bare, apps)))


def _place_match(text: str, place_names: tuple[str, ...], apps: dict[str, str] | None = None,
                 take_me: bool = True):
    """The whole place form of one utterance: ("place", dict) | ("geo", target words) | None.
    `take_me=False` skips "take me to <x>": parse tries it only after every other kind (round 1)."""
    if len(text) > 200:
        return None
    t = destutter(_strip_tail(strip_lead(_clean(text)))).strip().rstrip(".?!,").strip()
    low = t.lower().replace("’", "'").replace(",", "")
    for rx in _WHATS_ON_RES:
        m = rx.fullmatch(low)
        if m:
            return ("place", {"mode": "whats_on", "query": "cinema", "film": "", "when": (m["when"] or " today")[1:]})
    m = _FILM_RE.fullmatch(low)
    if m and (m["place"] or m["when"] or m["verb"] != "on"):
        film = " ".join(m["film"].split())
        bare = re.sub(r"^(?:the|a|an) ", "", film)
        if (film and len(film) <= 60 and _PLACE_WORDS_RE.fullmatch(film) and bare not in _FILM_STOP
                and film not in _FILM_STOP and not re.search(r"\b(?:line|tube|bus|train)$", film)):
            return ("place", {"mode": "film", "query": "cinema", "film": film, "when": (m["when"] or " today")[1:]})
        return None
    m = _TAKE_ME_RE.fullmatch(low) if take_me else None
    if m:
        x = m["home"] or m["x"].strip()
        if x.startswith("my ") and place_name(x) != "home" and place_name(x) not in place_names:
            return None  # "take me to my notes": only a saved "my" name maps
        hit = _place_target(x, place_names) if _take_me_target(x, apps or {}) else None
        if hit is None:
            return None
        return hit if hit[0] == "geo" else ("place", {"mode": hit[1], "query": hit[2], "film": "", "when": "today"})
    for rx in _PLACE_BEST_RES:
        m = rx.fullmatch(low)
        if not m:
            continue
        if "bare" in m.groupdict() and m["bare"] is not None:
            word = m["bare"].strip()
            if word not in _CINEMA_WORDS and word not in CATEGORY:
                return None  # "best way to cook rice": not a place
            x = word
        else:
            x = m["x"]
            if re.search(r"\b(?:to|from|than)\b", x):  # "the closest planet to the sun": a question, not a place
                return None
        hit = _place_target(x, place_names)
        if hit is None:
            return None
        return hit if hit[0] == "geo" else ("place", {"mode": hit[1], "query": hit[2], "film": "", "when": "today"})
    m = _PLACE_FIND_RE.fullmatch(t)
    if m and not _FIND_MEDIA_RE.search(m["x"]):
        x = m["x"].strip()
        # "find me a Pret" (a capitalised name, <= 3 words) or a category word; "find me a picture of a cat"
        # and other lower-case asks stay with the media and answer kinds.
        if x.lower() in CATEGORY or (x[:1].isupper() and len(x.split()) <= 3):
            hit = _place_target(x.lower(), place_names)
            if hit and hit[0] == "place":
                return ("place", {"mode": hit[1], "query": hit[2], "film": "", "when": "today"})
    return None


def place_request(text: str, place_names: tuple[str, ...] = (), apps: dict[str, str] | None = None) -> dict | None:
    """{"mode": name|category|cinema|film|whats_on, "query", "film", "when": today|tonight|tomorrow} or None.
    Fixed code, owner words only; a saved name, home or a postcode is stage B's route (place_geo_request).
    `apps`: installed apps, so "take me to Safari" is never a place."""
    hit = _place_match(text, place_names, apps)
    return hit[1] if hit and hit[0] == "place" else None


def place_geo_request(text: str, place_names: tuple[str, ...] = ()) -> GeoAsk | None:
    """"best way to go to the gym / home / SW1A 2AA" -> stage B's map route from home."""
    hit = _place_match(text, place_names)
    return GeoAsk("geo_map", "", hit[1]) if hit and hit[0] == "geo" else None


@dataclass
class VideoAsk:
    kind: str       # video_ask | video_watch
    question: str   # the owner's question ("" for watch)
    video_id: str   # from a YouTube link in the words, else "" (the conversation's last video)


_VIDEO_REF_RE = re.compile(r"\b(?:the|this|that) video\b", re.I)
_URL_RE = re.compile(r"https?://\S+", re.I)
_QUESTION_LEAD_RE = re.compile(r"^(?:what|how|does|did|do|who|when|where|why|which|is|are|can|could|tell me"
                               r"|according to|in)\b", re.I)
_VIDEO_WATCH_RE = re.compile(r"(?:watch|summari[sz]e) (?:it|the video|that video|this video)"
                             r"|give me a summary of (?:it|the video|that video|this video)", re.I)
_YOUTUBE_HOSTISH_RE = re.compile(r"https?://[^\s/]*youtu", re.I)
_VIDEO_WATCH_URL_RE = re.compile(r"(?:watch|summari[sz]e) (?P<url>https?://\S+)", re.I)


def _youtube_id(text: str) -> str:
    from common import hud  # lazy: intents stays importable without the shared package path
    for url in _URL_RE.findall(text or ""):
        vid = hud.youtube_id_from_url(url.rstrip(".,?!"))
        if vid:
            return vid
    return ""


def video_request(text: str) -> VideoAsk | None:
    """"watch it" / "summarise the video" (video_watch), or a question about the/this/that video or a
    YouTube link (video_ask). The id comes only from a link in the owner's own words."""
    if len(text) > 300:
        return None
    t = _clean(text)
    low = t.lower()
    for form in _match_forms(low):
        if _VIDEO_WATCH_RE.fullmatch(form):
            return VideoAsk("video_watch", "", "")
    m = _VIDEO_WATCH_URL_RE.fullmatch(strip_lead(t))
    if m:
        return VideoAsk("video_watch", "", _youtube_id(m["url"]))
    has_url = bool(_YOUTUBE_HOSTISH_RE.search(t))  # a YouTube (or lookalike) link; other links stay answers
    if (_VIDEO_REF_RE.search(t) or has_url) and _QUESTION_LEAD_RE.match(strip_lead(t)):
        return VideoAsk("video_ask", t[:300], _youtube_id(t))
    return None
