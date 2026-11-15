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
    events = turn(client, "remove the cat", conv_id="j9b", followup=True, levels=[0.002] * 12, conf=0.9)
    assert not _drops(events) and _last("followup_dropped")["reason"] == "level"  # names-only, quiet


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


def test_short_delete_followup_never_reaches_a_saved_workspace(client, monkeypatch, tmp_path):
    """Security R1: "delete the cat" with a saved workspace "cat" and a Cat card shown parses as a
    workspace delete at dispatch. The short-rule exemption is for a card remove only: it stays dropped."""
    from mac.voice import workspaces
    monkeypatch.setenv("OVERLAY_WORKSPACE_DIR", str(tmp_path / "ws"))
    workspaces.save("cat", [{"id": "old", "type": "info", "title": "Old"}], [], None)
    clock = Clock()
    _deps(clock=clock, chunks=['{"say": "Here."}\n'])
    _owner(client, "j9w")
    _seed("j9w", clock)
    events = turn(client, "delete the cat", conv_id="j9w", followup=True, levels=[0.049] * 12, conf=0.9)
    assert _last("followup_dropped")["reason"] == "short"
    assert says(events) == [] and not _drops(events)
    assert not [e for e in events if e["type"] == "card"]  # no "Delete cat. Shall I?" Confirm
    assert "workspace" not in [r.get("kind") for r in _steps("intent")]
    assert "cat" in overlay._ws_names()  # the file is still there
    # with no workspace of that name the same words are a card remove, as before
    events = turn(client, "delete the moon", conv_id="j9w", followup=True, levels=[0.049] * 12, conf=0.9)
    assert _drops(events) == [["k2"]]


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


def test_spoken_name_drops_lead_article_and_dangling_word(client):
    # Mac live run a (round 2): "The Roman Empire: key facts" was spoken "the the roman empire key".
    clock = Clock()
    _deps(clock=clock)
    for title, want in [("The Roman Empire: key facts", "Removed the roman empire key facts, sir."),
                        ("A cat on a mat", "Removed the cat on a mat, sir."),
                        ("Rise and fall of Rome", "Removed the rise and fall, sir."),
                        ("Notes on the Roman Empire", "Removed the notes on the roman, sir."),
                        ("The", "Removed the card, sir.")]:
        _seed("j9r", clock, [("info", title)])
        events = turn(client, "remove that card", conv_id="j9r")
        assert says(events) == [want]
    _seed("j9s", clock, [("info", "The Roman Empire: key facts"), ("image", "A cat")])
    events = turn(client, "remove the dog card", conv_id="j9s")
    assert says(events) == ["I don't see that card. On screen: the cat and the roman empire key facts."]
    _seed("j9t", clock, [("info", "The Rome history"), ("info", "The Rome facts")])
    events = turn(client, "remove the Rome card", conv_id="j9t")
    assert says(events)[0].startswith("Which one: the rome facts or the rome history?")
    assert not any("the the" in s.lower() for s in says(events))
    overlay._card_pick.pop("j9t", None)


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
    assert says(events) == [acks.PHRASES["companion_about"]]  # no thinking ack before it
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


# ---- round 5 (owner voice test 2026-11-15 20:56-21:03 UTC) ----

# The two owner rows that got the canned Companion line (transcripts as the ring kept them).
_ROW_210031 = ("That's fine. The rotor automatically goes to the miniBC. Remember that for next sessions. "
               "And sort a session on the router so that uh, Jarvis companion could use clicks uh, "
               "for agents and for you as t")
_ROW_210224 = ("start a session, uh, to help the Jarvis companion app, uh, allow AI agents to use remote "
               "control for clicks on all of my devices, including the mini PC, the Mac and the normal PC")


def _turn_kinds(client, text, conv):
    """The intent kinds one turn logs when the fast brain wants the agent."""
    overlay.debug_log.clear()
    _deps(chunks=['{"agent": true}\n'])
    events = turn(client, text, conv_id=conv)
    return [s.get("kind") for s in _steps("intent")], events


@pytest.mark.parametrize("text", [_ROW_210031, _ROW_210224,
                                  "check the Jarvis Companion repo and add a capabilities page",
                                  "create a new session to fix the Jarvis Companion app"])
def test_companion_task_takes_the_agent_path(client, text):
    """F1: a task that only mentions the Companion goes on as the same sentence without it."""
    kinds, events = _turn_kinds(client, text, "j9u")
    plain, _ = _turn_kinds(client, re.sub(r"(?i)jarvis companion", "the app", text), "j9v")
    assert "about_companion" not in kinds and "agent" in kinds
    assert kinds == plain
    assert acks.PHRASES["companion_about"] not in says(events)
    assert not [e for e in events if e["type"] == "card" and e["card"].get("title") == "Jarvis Companion"]
    assert not about_companion(text)


@pytest.mark.parametrize("text", ["check Jarvis Companion capabilities", "Java's companion, what can it do",
                                  "what can Jarvis Companion do", "what is Jarvis Companion",
                                  "tell me about the companion app", "what does the Jarvis companion include",
                                  "companion", "I meant the Jarvis companion"])
def test_companion_question_still_gets_the_companion_answer(client, text):
    """F1: questions about what the Companion is or does keep the fixed answer, never the agent."""
    kinds, events = _turn_kinds(client, text, "j9w")
    assert "about_companion" in kinds and "agent" not in kinds
    assert says(events) == [acks.PHRASES["companion_about"]]


def test_answer_prompt_limits_the_companion_rule_to_questions():
    """F1: the prompt's no-agent rule covers questions about the Companion, not tasks naming it."""
    p = overlay.ANSWER_PROMPT
    assert "Questions about what Jarvis, Jarvis Companion or 'the companion' is or does" in p
    assert "A task that only mentions the Companion" in p


@pytest.mark.parametrize("text", ["Stop. Driver stop", "okay Jarvis stop", "ok stop", "Jarvis stop it",
                                  "hey Jarvis, stop please", "no no stop", "Driver stop", "stop it now jarvis",
                                  "okay, cancel that, jarvis"])
def test_stop_family_parses_as_stop(text):
    """F2: a short stop word with only fillers and at most one mis-heard word is the stop intent."""
    assert parse(text, apps={}).kind == "stop", text


@pytest.mark.parametrize("text,kind", [("what does stop loss mean", "answer"), ("don't stop", "answer"),
                                       ("stop and clear everything", "clear"), ("stop clear", "clear"),
                                       ("stop the music", "answer"), ("cancel the timer", "answer"),
                                       ("do not stop", "answer"), ("never stop", "answer")])
def test_stop_family_leaves_content_alone(text, kind):
    """F2: real content keeps its handling ("don't stop" asks to go on, so it is never a stop)."""
    assert parse(text, apps={}).kind == kind, text


def test_followup_stop_with_misheard_name_is_not_dropped(client):
    """F2: row 21:02:40 replayed: 'Stop. Driver stop' at level 0.089 conf 0.5 stops, no followup_dropped."""
    _deps(chunks=['{"say": "Here."}\n'])
    _owner(client, "j9x")
    overlay.debug_log.clear()
    events = turn(client, "Stop. Driver stop", conv_id="j9x", followup=True, levels=[0.089] * 12, conf=0.5)
    assert not _steps("followup_dropped")
    assert _last("intent")["kind"] == "stop"
    assert says(events) == [acks.PHRASES["stop"]]
    # As the exact "stop": a quiet unsure one is still a stop (the base exemption, no other gate opened).
    overlay.debug_log.clear()
    turn(client, "okay Driver stop", conv_id="j9x", followup=True, levels=[0.003] * 12, conf=0.339)
    assert _last("intent")["kind"] == "stop" and not _steps("followup_dropped")
    # A short follow-up with content stays room noise.
    overlay.debug_log.clear()
    turn(client, "stop the music", conv_id="j9x", followup=True, levels=[0.089] * 12, conf=0.5)
    assert _last("followup_dropped")["reason"] == "short"
