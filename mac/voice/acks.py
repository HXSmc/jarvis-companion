"""Spoken acknowledgements: one short line per intent, pre-rendered to PCM.

The ack is the first event of every turn, so the phrases are synthesized once
up front and cached; a missing or failing synthesizer degrades to the text
form and never blocks the turn.
"""

from __future__ import annotations

import collections
import os
import random
from collections.abc import Callable

PHRASES = {
    "open": "Of course, sir.",
    "look": "Having a look.",
    "diff": "Pulling up the changes.",
    "answer": "Just a moment.",
    "clear": "All cleared, sir.",
    "hide": "Hidden.",
    "show": "Showing them.",
    "aside": "Out of the way.",
    "restore": "Putting them back.",
    "move": "Moving them.",
    "generic": "On it.",
    "agent_git": "Checking the git history.",
    "agent_gh": "Looking at GitHub.",
    "agent_notes": "Searching your notes.",
    "agent_mail": "Checking your mail.",
    "agent_calendar": "Checking your calendar.",
    "agent_code": "Looking at that file.",
    "progress_git": "Reading the git log.",
    "progress_gh": "Checking GitHub.",
    "progress_notes": "Reading your notes.",
    "progress_file": "Reading the file.",
    "progress_mail": "Reading your mail.",
    "progress_calendar": "Reading your calendar.",
    "progress_other": "Still working on it.",
    "stopped": "Stopped.",
    "timeout": "That took too long, so I stopped.",
    "busy": "Still on the last one.",
    "not_confirmed": "Very well, I won't.",
    "expired": "That request expired, so I haven't done it.",
    "hunk": "Right.",
    "no_hunks": "There's no list of changes yet. Ask me to highlight them first.",
    "last_hunk": "That was the last change.",
    "first_hunk": "That's the first change.",
    "why_removed": "Blame can't trace removed lines. Say next change to move on.",
    "hunk_refused": "I won't run git in that repository, I'm afraid.",
    "diff_none_merge": "No changes since the last merge, and that merge changed nothing here.",
    "diff_none_auto": "No uncommitted changes here.",
    "diff_no_merge": "This repository has no merge commits yet.",
    "lines_off_view": "That change is folded or wrapped; I can't box it.",
    "no_repo": "I can't tell which repo. Open a file or Terminal in it.",
    # A template ({n} = the wanted line number only): spoken through the synth, never prerendered.
    # Any boxed line, not only a change (Mac live run 4: an agent's timer line was called "the change").
    "lines_short": "I see no line numbers here. It's line {n}.",
    # A template ({name} = the changed file's name): a Terminal-picked repo gets cards, no boxes.
    "cards_only": "Cards only. Open {name} in your editor to see the boxes.",
    "stop": "OK.",
    "enlarged": "Enlarged, sir.",
    "restored": "Back to normal.",
    "no_card": "I don't see a card called that.",
    "no_sessions_access": "I can't reach your sessions.",
    "sessions": "Checking your sessions.",
    "no_sessions": "No sessions to report.",
    "no_session_named": "I don't see a session called that.",
    # jfollow4: session/memory/workspace helpers and the vault/cut-off guards.
    "session_none_named": "I don't see a session called that, sir.",
    "session_device": "Sessions start on the mini PC only, sir.",
    "session_failed": "That didn't go through, sir.",
    "session_limit": "That's the limit for this hour, sir.",
    "session_ask_gone": "That question is no longer open, sir.",
    "session_started_it": "Started it on the mini PC, sir.",
    "session_sent": "Sent.",
    "memory_none": "I don't have anything about that, sir.",
    "memory_empty": "I don't remember anything yet, sir.",
    "memory_forgot": "Forgotten, sir.",
    "memory_forgot_all": "All forgotten, sir.",
    "memory_failed": "I couldn't save that, sir.",
    "workspace_failed": "I couldn't save that one, sir.",
    "workspace_none": "I have nothing saved by that name.",
    "workspace_empty": "There's nothing on screen to save.",
    "workspace_full": "That's thirty saved. Delete one first.",
    "workspace_deleted": "Deleted, sir.",
    "workspace_none_saved": "You haven't saved anything yet, sir.",
    "workspace_no_pages": "No pages were saved there.",
    "vault_off_limits": "That folder is off limits, sir.",
    "cut_off": "I didn't catch the end of that, sir.",
}

# Thinking lines said while the model works: (key, text, tag). "look" = an answer/lookup is
# coming, "do" = an action, "any" = either. answer turns hear only look/any lines.
THINKING_POOL: tuple[tuple[str, str, str], ...] = tuple(
    (f"think_{i:02d}", text, tag)
    for i, (text, tag) in enumerate((
        ("I'll check that now, sir.", "look"),  # the 8-word line measured 2.03 s live (cap 1.8 s)
        ("Give me a second, sir.", "any"),
        ("Right away, sir.", "do"),
        ("Let me look into that.", "look"),
        ("One moment, sir.", "any"),
        ("Checking now.", "any"),
        ("On it, sir.", "do"),
        ("Let me pull that up.", "look"),
        ("Just a moment, sir.", "any"),
        ("Looking into it, sir.", "look"),
        ("Bear with me a second.", "any"),
        ("Working on it, sir.", "do"),
        ("Let me find out, sir.", "look"),
        ("One second.", "any"),
        ("Working on that now.", "do"),
    ), start=1)
)
PHRASES.update({key: text for key, text, _tag in THINKING_POOL})


def sir_rate() -> float:
    """Chance a thinking ack carries ", sir"; OVERLAY_SIR_RATE overrides at call time."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("OVERLAY_SIR_RATE", 0.67))))
    except (TypeError, ValueError):
        return 0.67


# Intent kind -> PHRASES key; None = the turn speaks without an ack first.
# "answer" resolves through the thinking pool, "agent" through pick_key(kind, sub).
ACK_POLICY: dict[str, str | None] = {
    "clock": None, "media": None, "stop": None, "card_size": None,
    "open_app": None, "open_site": None,
    "clear": "clear", "hide": "hide", "show": "show",
    "aside": "aside", "restore": "restore", "move": "move",
    "hunk": "hunk", "git_diff": "diff",
    "look": "look", "ground": "look",
    "agent": "agent",
    "sessions": "sessions",
    "session": None, "memory": None, "workspace": None,
    "answer": "answer",
}


def ack_key(kind: str, sub: str = "") -> str | None:
    """Intent kind -> the ack PHRASES key to say first; None = no ack, unknown = generic."""
    if kind == "agent":
        return pick_key("agent", sub)
    return ACK_POLICY.get(kind, "generic")


_ACK_KEYS = {
    "open_app": "open",
    "open_site": "open",
    "ground": "look",
    "look": "look",
    "git_diff": "diff",
    "hunk": "hunk",
    "clear": "clear",
    "answer": "answer",
    "hide": "hide",
    "show": "show",
    "aside": "aside",
    "restore": "restore",
    "move": "move",
}


def pick_key(intent_kind: str, sub: str = "") -> str:
    """Intent kind -> PHRASES key; unknown kinds get the generic ack."""
    if intent_kind == "agent":
        return f"agent_{sub}" if f"agent_{sub}" in PHRASES else "agent_code"
    return _ACK_KEYS.get(intent_kind, "generic")


def progress_key(kind: str) -> str:
    """Agent sub -> progress-line key; unknown subs get the generic progress line."""
    return f"progress_{kind}" if f"progress_{kind}" in PHRASES else "progress_other"


class AckCache:
    """Prerendered ack clips; get() always returns a speakable (text, pcm, rate)."""

    def __init__(self, synth: Callable[[str], tuple[bytes, int]] | None = None,
                 rng: random.Random | None = None):
        # synth(text) -> (pcm bytes, rate); None = text-only acks.
        self._synth = synth
        self._clips: dict[str, tuple[bytes, int]] = {}
        self._rng = rng or random.Random()
        self._recent: collections.deque[str] = collections.deque(maxlen=3)  # last thinking keys

    def prerender(self) -> None:
        """Render every phrase once; a phrase whose synth raises stays text-only."""
        if self._synth is None:
            return
        for key, text in PHRASES.items():
            if "{" in text:  # a template, filled in per use
                continue
            try:
                self._clips[key] = self._synth(text)
            except Exception:
                continue

    def get(self, key: str) -> tuple[str, bytes | None, int]:
        if key not in PHRASES:
            key = "generic"
        pcm, rate = self._clips.get(key, (None, 0))
        return PHRASES[key], pcm, rate

    def thinking(self, kind: str, rng: random.Random | None = None) -> tuple[str, str, bytes | None, int]:
        """A thinking ack for the turn: (key, text, pcm, rate), none of the last three again."""
        allowed = ("look", "any") if kind == "answer" else ("do", "any")
        r = rng or self._rng
        rate = sir_rate()
        lines = [e for e in THINKING_POOL if e[2] in allowed and e[0] not in self._recent]
        sir = [e for e in lines if ", sir" in e[1]]
        plain = [e for e in lines if ", sir" not in e[1]]
        wanted = sir if r.random() < rate else plain
        if not wanted and 0 < rate < 1:
            wanted = plain or sir  # the style ran dry: the other one beats silence
        if not wanted:  # pinned rate: the style is required, a repeat beats the wrong style
            lines = [e for e in THINKING_POOL if e[2] in allowed
                     and ((", sir" in e[1]) == bool(rate))]
            wanted = lines or list(THINKING_POOL)
        key, text, _tag = r.choice(wanted)
        self._recent.append(key)
        return key, *self.get(key)
