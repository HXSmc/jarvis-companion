"""jfollow9: destutter, stop+clear, one-card removal by position/name/kind, companion alias."""

from __future__ import annotations

from mac.voice import acks
from mac.voice.intents import (about_companion, card_matches, card_remove_request,
                               destutter, first_form, parse)

CARDS = [("c1", "info", ("ottoman", "empire", "milestones")),
         ("c2", "image", ("cat",)),
         ("c3", "video", ("moon", "landing", "footage")),
         ("c4", "info", ("notes", "on", "rome"))]


def _target(t: str) -> str | None:
    """The one-card name a phrase asks to remove: the parse's own target, else the ask."""
    p = parse(t, apps={})
    if p.kind == "card_remove":
        return p.sub
    ask = first_form(t, card_remove_request)
    return ask.target if ask else None


def test_stutter_and_combined_clear_parse():
    # A stuttered word that starts the next word's letters is dropped; an immediate
    # repeat keeps its second copy; clean text is untouched.
    assert destutter("clear the over, overlay") == "clear the overlay"
    assert destutter("remove the the cat card") == "remove the cat card"
    assert destutter("what time is it") == "what time is it"
    # "stop and clear" rides into the clear intent ("Okay, okay, " is a stripped lead).
    assert parse("Okay, okay, stop and clear everything", apps={}).kind == "clear"
    assert parse("stop and clear everything", apps={}).kind == "clear"
    # The wider remove-everything alternatives; the plain close-it/that/this stays clear.
    assert parse("remove everything", apps={}).kind == "clear"
    assert parse("close all the cards", apps={}).kind == "clear"
    assert parse("delete everything", apps={}).kind == "clear"
    assert parse("close it", apps={}).kind == "clear"
    assert parse("close that", apps={}).kind == "clear"
    assert parse("close this", apps={}).kind == "clear"


def test_card_words_parse():
    # Each phrase resolves to exactly one card, by position, title (with the STT
    # plural fold and possessive strip) or kind.
    wanted = [("remove the first card", "c1"), ("remove the second card", "c2"),
              ("remove the third card", "c3"), ("remove card number two", "c2"),
              ("remove the card about cats", "c2"), ("remove the cats card", "c2"),
              ("remove the Cat's card", "c2"), ("delete the cat card", "c2"),
              ("remove the the cat card", "c2"), ("get rid of the cat picture", "c2"),
              ("remove the card about the Ottoman empire", "c1"),
              ("close the video", "c3"), ("close the clip", "c3")]
    for t, cid in wanted:
        m = card_matches(_target(t), CARDS)
        assert m is not None and len(m[1]) == 1 and m[1][0] == cid, (t, m)
    for t in ("remove the fifth card", "remove the dog card"):
        assert card_matches(_target(t), CARDS) is None, t
    # Two cards sharing the spoken word: both come back, not one at random.
    two = [("a1", "info", ("rome", "history")), ("a2", "info", ("rome", "facts"))]
    m = card_matches(_target("remove the rome card"), two)
    assert m is not None and len(m[1]) == 2, m
    # A spoken kind with one card of that kind: that card.
    m = card_matches(_target("remove the info card"), [("b1", "info", ("rome", "history"))])
    assert m == ("kind", ["b1"]), m


def test_soft_kind_words_keep_parse():
    # The new kind words are too weak to turn "close the notes" into a card remove:
    # these parsed "answer" before jfollow9 and still do (measured on the base file).
    assert parse("close the notes", apps={}).kind == "answer"
    assert parse("remove the link", apps={}).kind == "answer"
    assert parse("close the clip", apps={}).kind == "answer"


def test_about_companion_aliases():
    for t in ("check Jarvis Companion capabilities",
              "Can you show me the capabilities of Java's companion for other users",
              "companion", "I meant the Jarvis companion", "what can the Jarvis company do",
              "tell me about the companion app", "Jarvis companions features"):
        assert about_companion(t), t
    for t in ("what can you do", "what are my sessions doing on my companion",
              "the companion one", "remove the companion capabilities card",
              "show me a card titled Jarvis capabilities"):
        assert not about_companion(t), t
    # The companion question itself still parses as a plain answer.
    assert parse("what can Jarvis Companion do", apps={}).kind == "answer"
    # The new ack keys exist, the answer line within the ack length cap.
    for key in ("removed_named", "no_card_named", "no_cards", "companion_about"):
        assert key in acks.PHRASES, key
    assert len(acks.PHRASES["companion_about"].split()) <= 12


def test_stutter_and_kind_words_keep_old_asks():
    """Builder review: the prefix stutter needs STT punctuation ("the car cards" is not one), and
    a saved workspace named with a kind word is still deleted as a workspace."""
    assert parse("remove the car cards", apps={}).kind != "clear"
    assert parse("Clear the over, overlay", apps={}).kind == "clear"
    for name in ("shopping list", "meeting notes"):
        assert parse(f"delete the {name}", apps={}, workspace_names=(name,)).kind == "workspace"
    assert parse("delete the cat card", apps={}, workspace_names=("cat",)).kind == "card_remove"


# ---- turn level (gateway/overlay.py): follow-up gate, removal lines, off-screen cards, companion ----

import re  # noqa: E402
import sys  # noqa: E402
from collections import deque  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from gateway import overlay  # noqa: E402
from mac.voice import intents  # noqa: E402
from test_overlay_agent import Clock, client, reset  # noqa: F401,E402
from test_overlay_jfollow4 import _deps, _steps  # noqa: E402
from test_overlay_service import says, turn  # noqa: E402

OWNER = [0.07] * 12
TITLES = [("info", "Ottoman Empire milestones"), ("image", "Cat"), ("video", "Moon landing footage"),
          ("info", "Notes on Rome")]


@pytest.fixture(autouse=True)
def fresh_debug_log():
    overlay.debug_log.clear()
    yield
    overlay.debug_log.clear()


def _seed(conv, clock, titles=TITLES):
    """The index a card turn leaves (send order, oldest first) and its screen time."""
    overlay._card_index[conv] = deque(((f"k{i}", kind, intents.card_words(title))
                                       for i, (kind, title) in enumerate(titles)), maxlen=12)
    overlay._screen_at[conv] = (clock, clock())  # read on the deps clock, as run_turn does


def _drops(events):
    return [e["ids"] for e in events if e["type"] == "drop_cards"]


def _last(step):
    """The newest row of `step` (debug_log keeps turns newest last)."""
    return _steps(step)[-1] if _steps(step)[-1:] else {}


def _owner(client, conv):
    turn(client, "what time is it", conv_id=conv, levels=OWNER, audio_ms=1500)


def test_followup_combined_clear(client):
    _deps(chunks=['{"say": "Here."}\n'])
    _owner(client, "j9a")
    events = turn(client, "Okay, okay, stop and clear everything", conv_id="j9a", followup=True,
                  levels=[0.0448] * 12, conf=0.9)
    assert events.count({"type": "clear"}) == 1
    assert says(events) == [acks.PHRASES["clear"]]


def test_followup_short_command_gate(client):
    clock = Clock()
    _deps(clock=clock, chunks=['{"say": "Here."}\n'])
    _owner(client, "j9b")
    events = turn(client, "Remove everything", conv_id="j9b", followup=True, levels=[0.049] * 12, conf=0.91)
    assert {"type": "clear"} in events and not _steps("followup_dropped")
    events = turn(client, "Remove everything", conv_id="j9b", followup=True, levels=[0.002] * 12, conf=0.91)
    assert {"type": "clear"} not in events and _last("followup_dropped")["reason"] == "level"
    events = turn(client, "Remove everything", conv_id="j9b", followup=True, levels=[0.049] * 12, conf=0.2)
    assert {"type": "clear"} not in events and _last("followup_dropped")["reason"] == "confidence"
    turn(client, "Stop", conv_id="j9b", followup=True, levels=[0.003] * 12, conf=0.339)
    assert _last("intent")["kind"] == "stop"
    _seed("j9b", clock)
    events = turn(client, "remove the cat card", conv_id="j9b", followup=True, levels=[0.002] * 12, conf=0.9)
    assert not _drops(events) and _last("followup_dropped")["reason"] == "level"


@pytest.mark.parametrize("text,want", [("remove the cat", "k1"), ("close the footage", "k2"),
                                       ("remove the notes", "k3")])
def test_short_names_only_remove_followup_acts(client, text, want):
    """A loud 3-word names-only remove parses as `answer`: the short-noise rule must not drop it."""
    clock = Clock()
    _deps(clock=clock, chunks=['{"say": "Here."}\n'])
    _owner(client, "j9s")
    _seed("j9s", clock)
    events = turn(client, text, conv_id="j9s", followup=True, levels=[0.049] * 12, conf=0.9)
    assert _drops(events) == [[want]] and not _steps("followup_dropped")
    # a 3-word names-only follow-up that names no shown card is still room noise
    turn(client, "remove the unicorn", conv_id="j9s", followup=True, levels=[0.049] * 12, conf=0.9)
    assert _last("followup_dropped")["reason"] == "short"


def test_barge_combined_on_speakers(client):
    _deps(chunks=['{"say": "Here."}\n'])
    _owner(client, "j9c")
    events = turn(client, "stop", conv_id="j9c", followup=True, barge_in=True, levels=OWNER, conf=0.9)
    assert {"type": "clear"} not in events and says(events) == []
    events = turn(client, "Jarvis, stop and clear everything", conv_id="j9c", followup=True, barge_in=True,
                  levels=OWNER, conf=0.9)
    assert events.count({"type": "clear"}) == 1 and len(says(events)) == 1


@pytest.mark.parametrize("text,want", [
    ("remove that card", 3), ("close this card", 3), ("remove it", 3), ("remove the last card", 3),
    ("remove the first card", 0), ("remove the second card", 1), ("remove the third card", 2),
    ("remove the card about cats", 1), ("get rid of the cat picture", 1), ("remove the cats card", 1),
    ("remove the Cat's card", 1), ("delete the cat card", 1), ("remove the the cat card", 1),
    ("remove the card about the Ottoman empire", 0), ("close the video", 2), ("close the clip", 2),
])
def test_remove_one_card_phrasings(client, text, want):
    clock = Clock()
    _deps(clock=clock)
    _seed("j9d", clock)
    events = turn(client, text, conv_id="j9d")
    assert _drops(events) == [[f"k{want}"]] and {"type": "clear"} not in events
    name = " ".join(TITLES[want][1].split()[:4])
    assert [s.lower() for s in says(events)] == [f"removed the {name}, sir.".lower()]
    row = _last("card_remove")
    assert row["detail"].startswith("removed match=") and row["detail"].endswith(" cards=1")


def test_remove_one_card_no_match_and_ties(client):
    clock = Clock()
    _deps(clock=clock)
    on_screen = "i don't see that card. on screen: the notes on rome, the moon landing footage and the cat."
    for text in ("remove the fifth card", "remove the dog card"):
        _seed("j9e", clock)
        events = turn(client, text, conv_id="j9e")
        assert not _drops(events) and [s.lower() for s in says(events)] == [on_screen]
    overlay._card_index.pop("j9e")
    events = turn(client, "remove the dog card", conv_id="j9e")
    assert says(events) == [acks.PHRASES["no_cards"]] and not _drops(events)
    assert _last("card_remove")["detail"] == "refused reason=empty"
    _seed("j9f", clock, [("info", "Rome history"), ("info", "Rome facts")])
    for text in ("remove the Rome card", "remove the info card", "remove the notes"):
        events = turn(client, text, conv_id="j9f")
        assert not _drops(events) and says(events)[0].startswith("Which one")
        overlay._card_pick.pop("j9f", None)
    _seed("j9g", clock, [("info", "Rome history"), ("image", "Cat")])
    events = turn(client, "remove the info card", conv_id="j9g")
    assert _drops(events) == [["k0"]]


def test_spoken_title_taints(client):
    clock = Clock()
    _deps(clock=clock)
    _seed("j9h", clock, [("info", "One two three four five six seven eight nine")])
    events = turn(client, "remove that card", conv_id="j9h")
    assert says(events) == ["Removed the one two three four, sir."]
    assert "j9h" in overlay._tainted
    _seed("j9i", clock)
    turn(client, "remove the dog card", conv_id="j9i")
    assert "j9i" in overlay._tainted


def test_offscreen_cards_are_not_targets(client):
    clock = Clock()
    _deps(clock=clock)
    _seed("j9j", clock)
    clock.now += 61  # the app's idle clear took them off screen
    for text in ("remove that card", "remove the first card", "remove the dog card"):
        events = turn(client, text, conv_id="j9j")
        assert not _drops(events) and says(events) == [acks.PHRASES["no_cards"]]
    turn(client, "show them", conv_id="j9j")  # the app brings them back
    events = turn(client, "remove the cat card", conv_id="j9j")
    assert _drops(events) == [["k1"]]
    _seed("j9k", clock)
    overlay._enlarged["j9k"] = "k0"  # an enlarged card holds the idle clear
    clock.now += 61
    events = turn(client, "remove the cat card", conv_id="j9k")
    assert _drops(events) == [["k1"]]


@pytest.mark.parametrize("text", ["check Jarvis Companion capabilities", "companion"])
def test_companion_question_never_goes_to_agent(client, text):
    _deps(chunks=['{"agent": true}\n'])
    events = turn(client, text, conv_id="j9l")
    assert not _steps("repo_pick") and not [s for s in _steps("intent") if s.get("kind") == "agent"]
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert [c["title"] for c in cards] == ["Jarvis Companion"]
    body = " ".join(cards[0]["body"])
    for fact in ("iPhone app", "Gateway", "Mac overlay", "Claude Code sessions", "Windows", "Remote screen"):
        assert fact in body
    # criterion 21: one line == companion_about (the answer turn's thinking ack comes before the
    # model hands over; it is not a companion line)
    assert says(events).count(acks.PHRASES["companion_about"]) == 1 and len(says(events)) <= 2
    assert [s for s in _steps("intent") if s.get("kind") == "about_companion"]


def test_companion_notes_question_still_goes_to_agent(client):
    _deps(chunks=['{"agent": true}\n'])
    turn(client, "what's in my notes about the companion", conv_id="j9m")
    assert [s for s in _steps("intent") if s.get("kind") == "agent"]
    assert not [s for s in _steps("intent") if s.get("kind") == "about_companion"]


def test_prompt_holds_companion_paragraph():
    assert overlay.COMPANION_ABOUT in overlay.ANSWER_PROMPT
    assert overlay.COMPANION_ABOUT in overlay.QUIET_PROMPT
    assert 'never with {"agent": true}' in overlay.ANSWER_PROMPT
    assert len(overlay.COMPANION_ABOUT) <= 700
    # The criterion-22 pattern, built in parts so the repo's own privacy grep does not match it.
    private = "|".join([r"ts\.net", r"192\.168\.", r"100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.",
                        "/" + "Users/", "/" + "home/", "@", "token"])
    assert re.search(private, overlay.COMPANION_ABOUT) is None


def test_debug_page_rows_jfollow9():
    """The debug page labels the barge_check row and shows the barge_in first-try numbers whole."""
    import json
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    page = (Path(__file__).resolve().parent.parent / "gateway" / "overlay_debug.html").read_text()
    label = re.search(r"const LABEL = \{.*?\};", page, re.S).group(0)
    describe = re.search(r"function describe\(s\) \{.*?\n\}\n", page, re.S).group(0)
    check = ("res=reject why=no_hold rule=none over=1 cand=0 run=1 win=1 last_over_ms=300 mic=0.031 "
             "out=0.120 coup=0.050 ratio=4.10 fresh=0 words=3 new=3 name=1 stop=1")
    barge = ("at=7827 reason=aec rule=stop dropped_sentences=2 stop_ms=0 stream_open=0 "
             "voice_to_stop_ms=223 cmd_to_stop_ms=1200 cmd_rejects=0")
    rows = [{"step": "barge_check", "detail": check}, {"step": "barge_in", "detail": barge}]
    script = (f"{label}\n{describe}\nconsole.log(JSON.stringify({json.dumps(rows)}"
              ".map(r => [LABEL[r.step], describe(r)])));")
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == [["barge-in check", check], ["barge-in", barge]]
