"""jfollow6: card remove picks the exact name; the "Which one" answer goes back to the card
command; the model never claims an overlay change it did not make; more sessions phrasings."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from gateway import overlay  # noqa: E402
from mac.voice import acks  # noqa: E402
# The agent tests' fixtures (`reset` is autouse: stores cleared, HOME = a fake folder).
from test_overlay_agent import Clock, client, reset  # noqa: F401,E402
from test_overlay_jfollow2 import NEAR, OWNER, ROOM, lines  # noqa: E402
from test_overlay_jfollow4 import _deps, _steps  # noqa: E402
from test_overlay_jfollow5 import _show_cards  # noqa: E402
from test_overlay_service import FakeConversations, says, turn  # noqa: E402


def test_jfollow6_phrases():
    assert acks.ack_key("card_help") is None
    rendered = []

    def fake_synth(text):
        rendered.append(text)
        return b"pcm", 24000

    cache = acks.AckCache(synth=fake_synth)
    cache.prerender()
    for key in ("removed_both", "one_at_a_time", "card_help", "card_hint"):
        text = acks.PHRASES[key]
        assert len(text.split()) <= 8, key
        assert "?" not in text and "!" not in text, key
        assert text in rendered, key
        assert cache.get(key) == (text, b"pcm", 24000), key


# --- A + B service turns (criteria 6-8, 11-15) ---

NESTED = ("Jarvis companion capabilities", "Jarvis capabilities")
EQUAL = ("Jarvis companion capabilities", "Jarvis overlay capabilities")
_CARD_STORES = ("_card_index", "_enlarged", "_card_pick", "_host_ask")


@pytest.fixture(autouse=True)
def fresh_card_state(monkeypatch):
    """Card stores cleared per test; across the file the Confirm card's id is never dropped."""
    for name in _CARD_STORES:
        getattr(overlay, name).clear()
    seen: list[str] = []

    def watch(result):
        if result:
            seen.extend(result.get("ids", ()))
        return result

    real_saved, real_drop = overlay._drop_saved_cards, overlay._drop_cards
    monkeypatch.setattr(overlay, "_drop_saved_cards", lambda c, i: watch(real_saved(c, i)))
    monkeypatch.setattr(overlay, "_drop_cards", lambda c, g, keep=(): watch(real_drop(c, g, keep)))
    yield
    assert overlay._CONFIRM_CARD_ID not in seen
    for name in _CARD_STORES:
        getattr(overlay, name).clear()


def _model_deps(clock, n=4):
    """Deps whose model would answer; returns both pools so a test can count model calls."""
    loud = FakeConversations(lines(*[{"say": "Model answer."}] * n))
    quiet = FakeConversations(lines(*[{"say": "Model answer."}] * n))
    _deps(clock, conversations=loud, quiet_conversations=quiet)
    return loud, quiet


def _model_calls(*pools):
    return sum(len(s.prompts) for p in pools for s in p.sessions.values())


def _drops(events):
    return [e["ids"] for e in events if e["type"] == "drop_cards"]


def _held(conv):
    return [c[0] for c in overlay._card_index.get(conv, ())]


def _ask_which(client, conv, verb="remove", titles=EQUAL):
    """Cards up, then the ambiguous ask; returns ({title: id}, clock, loud, quiet)."""
    ids = _show_cards(client, conv, *titles)
    clock = Clock()
    loud, quiet = _model_deps(clock)
    events = turn(client, f"{verb} the jarvis capabilities card", conv_id=conv)
    assert len(says(events)) == 1 and says(events)[0].startswith("Which one: "), says(events)
    assert says(events)[0].endswith(f"? {overlay.ASK_AGAIN}")
    assert not _drops(events)
    return ids, clock, loud, quiet


def _spoken_order(conv, ids):
    """The pick's titles in the order its line named them."""
    by_id = {v: k for k, v in ids.items()}
    return [by_id[i] for i in overlay._card_pick[conv].ids]


@pytest.mark.parametrize("order", [NESTED, NESTED[::-1]])
def test_remove_exact_title_turn(client, order):
    clock = Clock()
    ids = _show_cards(client, "rx", *order)
    loud, quiet = _model_deps(clock)
    events = turn(client, "Remove the Jarvis capabilities card", conv_id="rx")
    exact, other = ids["Jarvis capabilities"], ids["Jarvis companion capabilities"]
    assert _drops(events) == [[exact]]
    assert says(events) == ["Removed, sir."]
    row = _steps("card_remove")[-1]
    assert (row["card"], row["match"]) == (exact, "title")
    assert _held("rx") == [other]
    assert _model_calls(loud, quiet) == 0


def test_rank_before_tiebreaks(client):
    clock = Clock()
    ids = _show_cards(client, "rt", *NESTED)
    _model_deps(clock)
    companion, exact = ids["Jarvis companion capabilities"], ids["Jarvis capabilities"]
    turn(client, "enlarge the jarvis companion capabilities card", conv_id="rt")
    assert overlay._enlarged["rt"] == companion
    events = turn(client, "remove the jarvis capabilities card", conv_id="rt")
    assert _drops(events) == [[exact]]  # rank wins over the enlarged-card tie-break
    assert _held("rt") == [companion]


def test_enlarge_ranked_and_ambiguous(client):
    clock = Clock()
    ids = _show_cards(client, "en", *NESTED)
    _model_deps(clock)
    events = turn(client, "enlarge the jarvis capabilities card", conv_id="en")
    exact = ids["Jarvis capabilities"]
    assert [e for e in events if e["type"] == "card_size"] == [
        {"type": "card_size", "id": exact, "size": "max"}]
    row = _steps("card_size")[-1]
    assert (row["action"], row["card"], row["match"]) == ("max", exact, "title")

    _ask_which(client, "ea", verb="enlarge")
    assert _steps("card_size")[-1]["detail"] == "refused reason=ambiguous"
    assert "ea" in overlay._tainted
    assert overlay._card_pick["ea"].action == "enlarge"
    assert "ea" not in overlay._enlarged

    overlay._card_index["ep"] = deque([("pic1", "image", ("cat",)), ("pic2", "image", ("dog",))],
                                      maxlen=12)
    events = turn(client, "enlarge the picture", conv_id="ep")
    assert [e["id"] for e in events if e["type"] == "card_size"] == ["pic2"]  # newest, unchanged


@pytest.mark.parametrize("reply", ["the companion one", "the second one", "both"])
def test_pick_removes_named_card(client, reply):
    ids, clock, loud, quiet = _ask_which(client, "pk")
    order = _spoken_order("pk", ids)
    assert sorted(order) == sorted(EQUAL)
    want = {"the companion one": ["Jarvis companion capabilities"], "the second one": order[1:],
            "both": order}[reply]
    events = turn(client, reply, conv_id="pk", followup=True, levels=NEAR)
    assert _drops(events) == [[ids[t] for t in want]]
    row = _steps("card_remove")[-1]
    if len(want) == 1:
        assert says(events) == ["Removed, sir."]
        assert (row["card"], row["match"]) == (ids[want[0]], "pick")
    else:
        assert says(events) == [acks.PHRASES["removed_both"]]
        assert (row["cards"], row["match"]) == (2, "pick")
    assert _model_calls(loud, quiet) == 0
    assert "pk" not in overlay._card_pick


def test_pick_enlarges_named_card(client):
    ids, clock, loud, quiet = _ask_which(client, "pe", verb="enlarge")
    events = turn(client, "both", conv_id="pe", followup=True, levels=NEAR)
    assert says(events) == [acks.PHRASES["one_at_a_time"]]
    assert not [e for e in events if e["type"] == "card_size"]
    assert "pe" in overlay._card_pick and "pe" not in overlay._enlarged  # pick kept
    events = turn(client, "the overlay one", conv_id="pe", followup=True, levels=NEAR)
    overlay_id = ids["Jarvis overlay capabilities"]
    assert [e for e in events if e["type"] == "card_size"] == [
        {"type": "card_size", "id": overlay_id, "size": "max"}]
    assert says(events) == [acks.PHRASES["enlarged"]]
    assert overlay._enlarged["pe"] == overlay_id
    row = _steps("card_size")[-1]
    assert (row["action"], row["card"], row["match"]) == ("max", overlay_id, "pick")
    assert _model_calls(loud, quiet) == 0


def test_pick_cleared(client):
    # (a) another intent runs as usual and ends the pick
    _ask_which(client, "c1")
    events = turn(client, "what time is it", conv_id="c1")
    assert events and "c1" not in overlay._card_pick
    assert not _drops(events)
    # (b) expired: a normal answer turn, the model called once
    ids, clock, loud, quiet = _ask_which(client, "c2")
    clock.now += overlay.CARD_PICK_S + 1
    events = turn(client, "the companion one", conv_id="c2")
    assert not _drops(events) and "c2" not in overlay._card_pick
    assert _model_calls(loud, quiet) == 1
    # (c) a clear ends it
    _ask_which(client, "c3")
    turn(client, "clear", conv_id="c3")
    assert "c3" not in overlay._card_pick
    # (d) a reference that names no offered card: no_card, no model
    ids, clock, loud, quiet = _ask_which(client, "c4")
    events = turn(client, "the purple one", conv_id="c4", followup=True, levels=NEAR)
    assert says(events) == [acks.PHRASES["no_card"]]
    assert not _drops(events) and _model_calls(loud, quiet) == 0
    assert "c4" not in overlay._card_pick
    # (e) _forget drops it
    _ask_which(client, "c5")
    overlay._forget("c5")
    assert "c5" not in overlay._card_pick
    # (f) the picked card left the index meanwhile: re-checked, no_card, nothing dropped.
    # (The spec's "remove the companion capabilities card" is itself a card_remove and ends the
    # pick by rule B, so the card leaves the index directly here.)
    ids, clock, loud, quiet = _ask_which(client, "c6")
    gone = ids["Jarvis companion capabilities"]
    overlay._card_index["c6"] = deque((c for c in overlay._card_index["c6"] if c[0] != gone),
                                      maxlen=12)
    events = turn(client, "the companion one", conv_id="c6", followup=True, levels=NEAR)
    assert says(events) == [acks.PHRASES["no_card"]] and not _drops(events)
    assert _model_calls(loud, quiet) == 0


def test_pick_gates(client):
    # a live pick: "both" is not dropped as short noise
    _ask_which(client, "g1")
    named = list(overlay._card_pick["g1"].ids)
    events = turn(client, "both", conv_id="g1", followup=True, levels=NEAR)
    assert _drops(events) == [named]
    assert not _steps("followup_dropped")
    # no pick: "both" as a follow-up is dropped as before
    clock = Clock()
    _model_deps(clock)
    events = turn(client, "both", conv_id="g2", followup=True, levels=NEAR)
    assert [e["type"] for e in events] == ["done"]
    assert _steps("followup_dropped")[-1]["reason"] == "short"
    # the level gate still applies to a pick reply
    turn(client, "what's the weather like", conv_id="g3", audio_ms=1800, levels=OWNER)
    ids, clock, loud, quiet = _ask_which(client, "g3")
    events = turn(client, "the second one", conv_id="g3", followup=True, audio_ms=3000,
                  levels=ROOM, conf=0.9)
    assert [e["type"] for e in events] == ["done"]
    assert _steps("followup_dropped")[-1]["reason"] == "level"
    assert len(_held("g3")) == 2
    # a pending host ask: the preflight settles it as today, nothing removed, the pick ends
    ids, clock, loud, quiet = _ask_which(client, "g4")
    overlay._host_ask["g4"] = overlay.HostAsk("stop", "Stop the voice test session", clock() + 30,
                                              {"id": "s1"})
    events = turn(client, "the companion one", conv_id="g4", followup=True, levels=NEAR)
    assert not _drops(events) and len(_held("g4")) == 2
    assert "g4" not in overlay._card_pick


def test_pick_ids_only_from_line(client):
    titles = ("Jarvis alpha capabilities", "Jarvis beta capabilities",
              "Jarvis gamma capabilities", "Jarvis delta capabilities")
    ids = _show_cards(client, "p4", *titles)
    clock = Clock()
    loud, quiet = _model_deps(clock)
    turn(client, "remove the jarvis capabilities card", conv_id="p4")
    pick = overlay._card_pick["p4"]
    assert len(pick.ids) == 3 and set(pick.ids) <= set(ids.values())
    left_out = (set(ids.values()) - set(pick.ids)).pop()
    events = turn(client, "the fourth one", conv_id="p4", followup=True, levels=NEAR)
    assert says(events) == [acks.PHRASES["no_card"]] and not _drops(events)
    turn(client, "remove the jarvis capabilities card", conv_id="p4")
    named = list(overlay._card_pick["p4"].ids)
    events = turn(client, "all", conv_id="p4", followup=True, levels=NEAR)
    assert _drops(events) == [named]
    assert _held("p4") == [left_out]
    assert _model_calls(loud, quiet) == 0


# --- C/D service tests (helper 2) ---

from test_overlay_agent import FakeAgent, FakeAgentPool, result, setup  # noqa: E402
from test_overlay_jfollow4 import _session_env  # noqa: E402
from test_overlay_service import POOL_TEXTS  # noqa: E402


def test_card_help_turn(client):
    """C1 / criterion 16: a 'can you remove one card' question helps, never deletes the newest."""
    ids = _show_cards(client, "ch", "Jarvis capabilities", "Jarvis companion capabilities")
    clock = Clock()
    loud, quiet = _model_deps(clock)
    events = turn(client, "can you remove one card", conv_id="ch")
    assert not _drops(events)
    assert set(_held("ch")) == set(ids.values())  # both cards still held
    assert events[0]["type"] == "say" and events[0]["text"] == acks.PHRASES["card_help"]
    assert says(events) == [acks.PHRASES["card_help"]]  # no ack audio/say before the fixed line
    assert _model_calls(loud, quiet) == 0
    assert _steps("intent")[-1]["kind"] == "card_help"


def test_answer_claim_held_and_dropped(client):
    """C2 / criterion 18: a false overlay claim is held, then dropped with the honest lines."""
    claim = "Understood, sir, clearing the capabilities card from the overlay now."

    def pools(*objs):
        clock = Clock()
        loud, quiet = FakeConversations(lines(*objs)), FakeConversations(lines(*objs))
        _deps(clock, conversations=loud, quiet_conversations=quiet)
        return loud, quiet

    # the claim then a gate-dropped clear: never spoken, honest lines instead, no clear event
    pools({"say": claim}, {"clear": True})
    events = turn(client, "the jarvis capabilities card", conv_id="cd1")
    spoken = says(events)
    assert claim not in " ".join(spoken) and claim not in str(events)
    assert spoken.count(overlay.OVERLAY_UNCHANGED_LINE) == 1
    assert spoken.count(acks.PHRASES["card_hint"]) == 1
    assert set(spoken) - {overlay.OVERLAY_UNCHANGED_LINE,
                          acks.PHRASES["card_hint"]} <= POOL_TEXTS  # only a thinking ack may lead
    assert not [e for e in events if e["type"] == "clear"]
    row = _steps("layout")[-1]
    assert (row["mode"], row["state"]) == ("clear", "dropped")
    assert _steps("answer")[-1]["claim"] == "dropped"

    # a claim with no directive after it: same outcome
    pools({"say": claim})
    events = turn(client, "the jarvis capabilities card", conv_id="cd2")
    spoken = says(events)
    assert claim not in " ".join(spoken)
    assert spoken.count(overlay.OVERLAY_UNCHANGED_LINE) == 1
    assert spoken.count(acks.PHRASES["card_hint"]) == 1
    assert _steps("answer")[-1]["claim"] == "dropped"

    # regression, hold-then-speak: the directive passes, so the claim is spoken just before it
    pools({"say": "Hiding the cards now, sir."}, {"layout": "hide"})
    events = turn(client, "hide the overlay for a bit", conv_id="cd3")
    say_i = next(i for i, e in enumerate(events)
                 if e["type"] == "say" and e["text"] == "Hiding the cards now, sir.")
    layout_i = next(i for i, e in enumerate(events) if e["type"] == "layout")
    assert say_i < layout_i
    assert overlay.OVERLAY_UNCHANGED_LINE not in says(events)

    # regression: today's order for a passing clear (as test_overlay_jfollow3.py:298)
    pools({"say": "Clearing the cards."}, {"clear": True})
    events = turn(client, "please wipe the screen for me now", conv_id="cd4")
    say_i = next(i for i, e in enumerate(events)
                 if e["type"] == "say" and e["text"] == "Clearing the cards.")
    assert say_i < next(i for i, e in enumerate(events) if e["type"] == "clear")


def test_model_clear_never_wipes_for_one_card(client):
    """C3 / criterion 19 turn part: one card the owner named survives a whole-screen wipe."""
    clock = Clock()
    loud = FakeConversations(lines({"say": "Done."}, {"clear": True}))
    quiet = FakeConversations()
    _deps(clock, conversations=loud, quiet_conversations=quiet)
    overlay._card_index["cw"] = deque([("k1", "info", ("weather", "report"))], maxlen=12)
    before = list(overlay._card_index["cw"])
    # Names-only: no held card answers to "jarvis thing", so it stays an answer turn, and its
    # "remove" passes the owner-verb gate for a model clear (the spec's second risk).
    events = turn(client, "remove the jarvis thing", conv_id="cw")
    assert not [e for e in events if e["type"] == "clear"]
    assert list(overlay._card_index["cw"]) == before
    row = _steps("layout")[-1]
    assert (row["mode"], row["state"], row["reason"]) == ("clear", "dropped", "one_card")
    assert "Done." in says(events)  # the non-claiming say line is unchanged
    assert overlay.OVERLAY_UNCHANGED_LINE in says(events) and acks.PHRASES["card_hint"] in says(events)

    # the C1 question never reaches the model at all
    calls = _model_calls(loud, quiet)
    events = turn(client, "can you not remove a single card", conv_id="cw2")
    assert _model_calls(loud, quiet) == calls and not _drops(events)

    # regression: whole-overlay asks still let the model clear, once each
    for j, phrase in enumerate(("close the overlay", "dismiss the overlay", "remove these")):
        loud = FakeConversations(lines({"say": "Done."}, {"clear": True}))
        _deps(clock, conversations=loud, quiet_conversations=FakeConversations())
        events = turn(client, phrase, conv_id=f"cr{j}")
        assert len([e for e in events if e["type"] == "clear"]) == 1


def test_model_clear_whole_overlay_asks_still_wipe(client):
    """Round 2: whole-overlay asks with a modifier or particle reach the model (as at base) and
    its clear still applies once: C3 only spares a card the owner named."""
    clock = Clock()
    for j, phrase in enumerate(("clear the whole screen", "clear up the screen",
                                "clear the entire overlay", "dismiss the whole overlay",
                                "close down the overlay", "clear every single card")):
        loud = FakeConversations(lines({"say": "Done."}, {"clear": True}))
        _deps(clock, conversations=loud, quiet_conversations=FakeConversations())
        overlay._card_index[f"cwo{j}"] = deque([("k1", "info", ("weather", "report"))], maxlen=12)
        events = turn(client, phrase, conv_id=f"cwo{j}")
        assert len([e for e in events if e["type"] == "clear"]) == 1, phrase
        assert overlay.OVERLAY_UNCHANGED_LINE not in says(events), phrase
    # a named card next to a whole-set word still survives a model wipe
    loud = FakeConversations(lines({"say": "Done."}, {"clear": True}))
    _deps(clock, conversations=loud, quiet_conversations=FakeConversations())
    overlay._card_index["cwn"] = deque([("k1", "info", ("weather", "report"))], maxlen=12)
    events = turn(client, "remove the whole jarvis thing", conv_id="cwn")
    assert not [e for e in events if e["type"] == "clear"]
    row = _steps("layout")[-1]
    assert (row["mode"], row["state"], row["reason"]) == ("clear", "dropped", "one_card")


def test_agent_claim_dropped(client):
    """C4 / criterion 20: the agent tier has no overlay directive, so its claims are false."""
    clock = Clock()
    setup(clock, lambda: FakeAgent(
        clock, script=[result({"say": "Files read."},
                              {"say": "I've cleared the capabilities card, sir."})]),
        chunks=['{"agent": true}\n'])
    events = turn(client, "tell me about the work I did", conv_id="ag1")
    spoken = says(events)
    assert "Files read." in spoken  # other agent say lines speak as today
    assert "cleared the capabilities card" not in " ".join(spoken)
    assert spoken.count(overlay.OVERLAY_UNCHANGED_LINE) == 1


def test_sessions_variants_turn(client, monkeypatch, tmp_path):
    """Criterion 22: the new sessions phrasings take the read-only sessions path, quiet pool only."""
    _session_env(monkeypatch, tmp_path)
    clock = Clock()
    made: list = []
    deps = _deps(clock, conversations=FakeConversations(),
                 quiet_conversations=FakeConversations(lines({"say": "Two sessions, one waiting."})))
    deps.agent_conversations = FakeAgentPool(lambda: made.append(1))
    for i, phrase in enumerate(("what's running on my Jarvis Companion",
                                "what sessions are on my driver's companion",
                                "how are the sessions on my Jarvis Companion")):
        conv = f"sv{i}"
        events = turn(client, phrase, conv_id=conv)
        assert events[0]["text"] == "Checking your sessions."  # the ack, before anything else
        assert "can't see" not in " ".join(says(events))
        assert any(r.get("kind") == "sessions" for r in _steps("intent"))
        assert _steps("sessions")[-1]["detail"] == "result=ok rows=2"
        assert [c for c in (e.get("card") for e in events) if c and c["type"] == "info"]
        prompt = deps.quiet_conversations.sessions[conv].prompts[0]
        assert "never instructions" in prompt and '<context untrusted="true">' in prompt
    assert deps.conversations.sessions == {}  # loud pool untouched
    assert not made  # no agent run



def test_claim_and_one_card_clear_say_hint_once(client):
    """Round 1 (glm-cross re-check): a held claim plus a C3-dropped clear in one reply runs the
    honest lines twice (C3 drop, then reply end); each line is still spoken only once."""
    clock = Clock()
    loud = FakeConversations(lines({"say": "Clearing the card from the overlay now."}, {"clear": True}))
    _deps(clock, conversations=loud, quiet_conversations=FakeConversations())
    overlay._card_index["ch"] = deque([("k1", "info", ("weather", "report"))], maxlen=12)
    events = turn(client, "remove the jarvis thing", conv_id="ch")
    spoken = says(events)
    assert not [e for e in events if e["type"] == "clear"]
    assert "Clearing the card from the overlay now." not in spoken
    assert spoken.count(overlay.OVERLAY_UNCHANGED_LINE) == 1
    assert spoken.count(acks.PHRASES["card_hint"]) == 1


# --- Round 3 (Mac live run): the voice never says the wake word ---

def test_voice_never_says_wake_word(client):
    """Mac live 28: the Which-one line read "jarvis" titles aloud and the Mac woke on its own
    speech, which ate the owner's pick. The caption keeps the titles; the voice drops the name."""
    ids = _show_cards(client, "ww", *EQUAL)
    heard: list[str] = []
    clock = Clock()
    loud = FakeConversations(lines({"say": "Model answer."}))
    _deps(clock, conversations=loud, quiet_conversations=loud,
          synth=lambda t: (heard.append(t), (b"\0\0", 24000))[1])
    events = turn(client, "remove the jarvis capabilities card", conv_id="ww")
    assert "jarvis" in says(events)[0]  # the caption names the cards as before
    assert heard and not any("jarvis" in h.lower() for h in heard), heard
    which = [h for h in heard if h.startswith("Which one")]
    assert len(which) == 1, heard
    assert "the companion capabilities" in which[0] and "the overlay capabilities" in which[0]
    events = turn(client, "the companion one", conv_id="ww", followup=True, levels=NEAR)
    assert _drops(events) == [[ids["Jarvis companion capabilities"]]]


@pytest.mark.parametrize("line, voice", [
    ("Two are active: the hardware mode remodel and Jarvis overlay session.",
     "Two are active: the hardware mode remodel and overlay session."),
    ("Jarvis: Companion Capabilities is up, sir.", "Companion Capabilities is up, sir."),
    ("Jarvis's cards, sir.", "cards, sir."),
    ("JARVIS, at your service.", "at your service."),
    ("No name here.", "No name here."),
])
def test_voice_text_drops_wake_word(line, voice):
    assert overlay._voice_text(line) == voice
    assert all("jarvis" not in p.lower() for p in acks.PHRASES.values())


def test_pronoun_claim_dropped_turn(client):
    """Mac live 30: a self-woken "overlapped" turn; the model's "I'll clear them" (card word in
    the sentence before) was spoken, then its clear was dropped. Now the claim never sounds."""
    claim = ("My apologies, sir, two cards landed on top of each other. "
             "I'll clear them and put up just one.")
    clock = Clock()
    objs = ({"say": claim}, {"clear": True},
            {"card": {"type": "info", "title": "Sessions", "body": ["x"]}})
    loud, quiet = FakeConversations(lines(*objs)), FakeConversations(lines(*objs))
    _deps(clock, conversations=loud, quiet_conversations=quiet)
    events = turn(client, "overlapped", conv_id="pc1")
    assert claim not in str(events)
    assert says(events).count(overlay.OVERLAY_UNCHANGED_LINE) == 1
    assert not [e for e in events if e["type"] == "clear"]
    assert [e["card"]["title"] for e in events if e["type"] == "card"] == ["Sessions"]
    assert _steps("answer")[-1]["claim"] == "dropped"


def test_name_only_line_has_no_voice():
    import asyncio
    from types import SimpleNamespace
    heard: list[str] = []
    deps = SimpleNamespace(synth=lambda t: (heard.append(t), (b"\0\0", 24000))[1])
    assert asyncio.run(overlay._audio("Jarvis.", deps)) is None
    assert asyncio.run(overlay._audio("Jarvis' cards.", deps))["text"] == "Jarvis' cards."
    assert heard == ["cards."]


def test_pick_by_the_name_as_heard_turn(client):
    """The voice said "the capabilities or the overlay capabilities": "the capabilities one" picks."""
    ids = _show_cards(client, "ah", "Jarvis capabilities", "Overlay capabilities")
    loud, quiet = _model_deps(Clock())
    events = turn(client, "remove the capabilities card", conv_id="ah")
    assert says(events)[0].startswith("Which one: ")
    events = turn(client, "the capabilities one", conv_id="ah", followup=True, levels=NEAR)
    assert _drops(events) == [[ids["Jarvis capabilities"]]]
    assert says(events) == ["Removed, sir."]
    assert _model_calls(loud, quiet) == 0


# --- Round 4 (Mac live 31c): the host may answer a start with the prompt itself as the title ---

def _echo_start(client, monkeypatch, tmp_path, cut, conv="vs1", sid="dd49a076-9af4"):
    """Start by voice; the host answers before its titler ran: the title is the prompt cut at `cut`."""
    overlay._voice_started.clear()
    overlay._voice_started_words.clear()
    overlay._session_calls.clear()  # the hourly start cap is per process: each test starts afresh
    monkeypatch.setattr(overlay, "FEEDBACK_S", 5.0)  # exact says() lists: no wall-clock filler line
    _deps(Clock())
    words = "say hello in a file called voice-test"
    title = overlay.session_prompt(words)[:cut]
    rows: list[dict] = [{"session_id": "s2", "state": "running", "device": "mini PC",
                         "title": "build runner", "last_activity_at": "2026-11-13T22:00:00Z"}]

    def post(request):
        if request.url.path == "/chat":
            rows.append({"session_id": sid, "state": "idle", "device": "mini PC", "title": title,
                         "last_activity_at": "2026-11-13T22:09:00Z"})
            return httpx.Response(200, json={"session_id": sid, "title": title})
        return httpx.Response(200, json={"ok": True})

    seen = _session_env(monkeypatch, tmp_path, rows=rows, post=post)
    turn(client, f"start a new session and {words}", conv_id=conv)
    events = turn(client, "yes", conv_id=conv)
    return events, seen, title, rows


@pytest.mark.parametrize("cut", [7, 16, 40, 55, 200])
def test_start_title_echoing_the_prompt_is_never_spoken(client, monkeypatch, tmp_path, cut):
    """31c: a title that is any prefix of the prompt we sent is not a title: the owner's words are."""
    events, _seen, title, _rows = _echo_start(client, monkeypatch, tmp_path, cut)
    assert title and "Started by voice"[:cut] in title
    spoken = says(events)
    assert spoken[-1] == acks.PHRASES["session_started_it"]
    assert not any("by voice" in s or f"Started {title}" in s for s in spoken)
    card = [e["card"] for e in events if e["type"] == "card"][-1]
    assert card["title"] == "say hello in a file called voice-test"
    assert card["body"] == ["Mini PC", "Started by voice"]


def test_start_real_title_still_spoken(client, monkeypatch, tmp_path):
    """A host title that is not the prompt (the titler ran) is spoken as before."""
    overlay._voice_started.clear()
    overlay._voice_started_words.clear()
    overlay._session_calls.clear()  # the hourly start cap is per process: each test starts afresh
    monkeypatch.setattr(overlay, "FEEDBACK_S", 5.0)  # exact says() lists: no wall-clock filler line
    _deps(Clock())

    def post(request):
        return httpx.Response(200, json={"session_id": "ns9", "title": "Started hello file"})

    _session_env(monkeypatch, tmp_path, post=post)
    turn(client, "start a session to fix the failing tests", conv_id="rt1")
    events = turn(client, "yes", conv_id="rt1")
    assert says(events) == ["Started Started hello file on the mini PC, sir."]


def test_stop_by_the_owner_start_words(client, monkeypatch, tmp_path):
    """31c: "stop the voice-test session" finds the session this overlay started with those words,
    names it by the owner's words, and still goes through the Confirm and the live list."""
    _events, seen, title, rows = _echo_start(client, monkeypatch, tmp_path, 40)
    events = turn(client, "stop the voice-test session", conv_id="vs2")
    assert says(events) == ["Stop the say hello in a file called voice-test session?"]
    assert not [u for m, u, _a, _b in seen if u.endswith("/stop")]  # nothing sent before yes
    events = turn(client, "yes", conv_id="vs2")
    assert says(events) == ["Stopped."]
    assert (seen[-1][0], seen[-1][1]) == (
        "POST", "http://127.0.0.1:8788/central/sessions/dd49a076-9af4/stop")
    # gone from the live list -> never stoppable by those words, even though this overlay started it
    rows.pop()
    events = turn(client, "stop the voice test session", conv_id="vs3")
    assert says(events) == [acks.PHRASES["session_none_named"]]
    assert _steps("session_stop")[-1]["detail"] == "refused reason=none_named"


def test_stop_words_never_match_a_session_this_overlay_did_not_start(client, monkeypatch, tmp_path):
    """The start words widen nothing: another session with the same echo title is not matched."""
    overlay._voice_started.clear()
    overlay._voice_started_words.clear()
    overlay._session_calls.clear()  # the hourly start cap is per process: each test starts afresh
    monkeypatch.setattr(overlay, "FEEDBACK_S", 5.0)  # exact says() lists: no wall-clock filler line
    _deps(Clock())
    title = overlay.session_prompt("say hello in a file called voice-test")[:40]
    _session_env(monkeypatch, tmp_path, rows=[{"session_id": "o1", "state": "idle", "title": title}])
    events = turn(client, "stop the voice-test session", conv_id="vs4")
    assert says(events) == [acks.PHRASES["session_none_named"]]


def test_stop_row_words_zero_is_the_start_field():
    """Round 4 item 4: a stop keeps its name in `name`; `words` is the start's own field (0 by design)."""
    from mac.voice import intents
    ask = intents.session_request("stop the voice-test session")
    assert (ask.action, ask.ref, ask.name, ask.words) == ("stop", "name", "voice-test", "")


def test_start_lookup_path_echo_title(client, monkeypatch, tmp_path):
    """31c on the timed-out start: the lookup's row title is the cut prompt too -> owner's words."""
    import json
    import time
    from test_overlay_jfollow5 import _slow_env
    overlay._voice_started.clear()
    overlay._voice_started_words.clear()
    overlay._session_calls.clear()
    _deps(Clock())
    rows: list = []

    def post(request):
        prompt = json.loads(request.read())["text"]
        rows[:] = [{"session_id": "new-7", "state": "working", "device": "Mini PC",
                    "title": prompt[:40], "task": prompt, "last_activity_at": "2026-11-13T22:09:00Z"}]
        time.sleep(overlay.FEEDBACK_S + 0.3)
        raise httpx.ReadTimeout("slow", request=request)

    _slow_env(monkeypatch, tmp_path, get=lambda: httpx.Response(200, json=rows), post=post)
    monkeypatch.setattr(overlay, "FEEDBACK_S", 0.2)
    turn(client, "start a session to fix the failing tests", conv_id="lk1")
    events = turn(client, "yes", conv_id="lk1")
    assert says(events) == [acks.PHRASES["session_waiting"], acks.PHRASES["session_started_it"]]
    assert [e["card"]["title"] for e in events if e["type"] == "card"][-1] == "fix the failing tests"
    assert _steps("session_start")[-1]["detail"] == "confirmed id=new-7 via=lookup"
