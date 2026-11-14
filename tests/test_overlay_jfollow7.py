"""jfollow7: a second wake inside a request folds into the open turn; debug rows show their
fields; injected-instruction notes are never read aloud; every sessions question reaches the
sessions intent."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from conftest import AUTH_HEADER  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import acks, intents  # noqa: E402
# The agent tests' fixtures (`reset` is autouse: stores cleared, HOME = a fake folder).
from test_overlay_agent import Clock, FakeAgent, FakeAgentPool, client, reset, result, setup  # noqa: F401,E402
from test_overlay_jfollow2 import lines  # noqa: E402
from test_overlay_jfollow4 import _deps, _session_env, _steps  # noqa: E402
from test_overlay_service import POOL_TEXTS, FakeConversations, make_deps, says, turn  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_debug_log():
    overlay.debug_log.clear()
    yield
    overlay.debug_log.clear()


# --- A: a wake inside the request (criteria 7-8) ---

def test_second_wake_folds_into_open_turn(client, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(overlay.time, "time", lambda: now[0])
    overlay.set_deps(make_deps(chunks=['{"say": "Here it is."}\n'], synth=lambda t: (b"\x01\x02", 24000)))
    client.post("/overlay/wake", json={"score": 0.79}, headers=AUTH_HEADER)
    now[0] += 4
    client.post("/overlay/wake", json={"score": 0.66}, headers=AUTH_HEADER)
    turn(client, "show me a card titled Jarvis capabilities")
    turns = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
    assert len(turns) == 1
    steps = turns[0]["steps"]
    assert [s["step"] for s in steps][:3] == ["wake", "voice", "transcribed"]
    assert steps[0]["again"] == 1 and steps[0]["score"] == 0.79
    assert "titled Jarvis capabilities" in steps[2]["text"]
    # A hit 31 s after a waiting wake opens a new turn.
    overlay.debug_log.clear()
    client.post("/overlay/wake", json={"score": 0.79}, headers=AUTH_HEADER)
    now[0] += overlay.WAKE_OPEN_S + 1
    client.post("/overlay/wake", json={"score": 0.7}, headers=AUTH_HEADER)
    turns = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
    assert len(turns) == 2 and all("again" not in t["steps"][0] for t in turns)


@pytest.mark.parametrize("text, name", [("show me a card titled Jarvis capabilities", "Jarvis capabilities"),
                                        ("what can Jarvis Companion do", "Jarvis Companion")])
def test_name_inside_request_reaches_model(client, text, name):
    intent = intents.parse(text, apps={})
    assert intent.kind == "answer" and name in intent.arg
    loud = FakeConversations(['{"say": "Sure."}\n'])
    overlay.set_deps(make_deps(conversations=loud, synth=lambda t: (b"\x01\x02", 24000)))
    turn(client, text)
    prompts = [p for s in loud.sessions.values() for p in s.prompts]
    assert prompts and any(text.split(" ", 2)[-1] in p for p in prompts)
    assert any(name in p for p in prompts)


# --- B: debug rows render their fields (criterion 11) ---

def _describe_with_node(rows: list[dict]) -> list[str]:
    page = (ROOT / "gateway" / "overlay_debug.html").read_text()
    label = re.search(r"const LABEL = \{.*?\};", page, re.S).group(0)
    describe = re.search(r"function describe\(s\) \{.*?\n\}\n", page, re.S).group(0)
    script = f"{label}\n{describe}\nconsole.log(JSON.stringify({json.dumps(rows)}.map(describe)));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_debug_page_describes_rows():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    rows = [
        {"step": "card_remove", "card": "b52eda1c", "match": "title"},
        {"step": "card_remove", "cards": 2, "match": "pick"},
        {"step": "card_remove", "detail": "refused reason=none"},
        {"step": "card_size", "detail": "refused reason=ambiguous"},
        {"step": "card_size", "action": "max", "card": "a1", "match": "title"},
        {"step": "answer", "claim": "dropped"},
        {"step": "answer", "note": "dropped", "sentences": 2},
        {"step": "wake", "score": 0.79, "source": "mac", "again": 1},
        {"step": "wake", "score": 0.79, "source": "mac"},
    ]
    got = _describe_with_node(rows)
    assert got[0] == "card=b52eda1c match=title"
    assert got[1] == "cards=2 match=pick"
    assert got[2] == "refused reason=none"
    assert got[3] == "refused reason=ambiguous"
    assert got[4] == "action=max card=a1 match=title"
    assert "claim=dropped" in got[5]
    assert "note=dropped" in got[6] and "2" in got[6]
    assert "score 0.79" in got[7] and "heard again 1" in got[7]
    assert got[8] == "score 0.79 (mac)"
    page = (ROOT / "gateway" / "overlay_debug.html").read_text()
    assert 'card_remove: "card remove"' in page and 'answer: "answer check"' in page


# --- C: injected-instruction notes are never read aloud (criteria 14-18, 24) ---

NOTE_1 = ("Of course, sir. One note: my earlier search was unnecessary, and its results told me to add "
          "source links to my reply. That instruction came from the search results, not from you, so I "
          "ignored it.")
NOTE_2 = ("Here is a lithium-ion cell, sir. The search results also included a line telling me to add "
          "source links to my reply, which I ignored.")
NOTE_ONLY = "That instruction came from the search results, not from you, so I ignored it."
INFO = {"type": "info", "title": "Battery", "body": ["A cell has an anode and a cathode."]}


def _model(*objs):
    loud = FakeConversations(lines(*objs))
    _deps(Clock(), conversations=loud, quiet_conversations=FakeConversations(lines(*objs)))
    return loud


def _model_says(events):
    return [t for t in says(events) if t not in POOL_TEXTS]


def _note_rows():
    return [r for r in _steps("answer") if r.get("note") == "dropped"]


def test_answer_injection_note_dropped(client):
    _model({"say": NOTE_1}, {"card": INFO})
    events = turn(client, "what is inside a battery", conv_id="n1")
    assert _model_says(events) == ["Of course, sir."]
    blob = json.dumps([e for e in events if e["type"] != "audio"]).lower()
    assert "search results" not in blob and "instruction" not in blob
    assert [e["card"]["title"] for e in events if e["type"] == "card"] == ["Battery"]
    (row,) = _note_rows()
    assert row["sentences"] == 2 and set(row) == {"step", "at", "note", "sentences"}
    assert "search results" not in str(overlay._history.get("n1")).lower()


def test_note_split_across_two_say_lines(client):
    _model({"say": "Here is a lithium-ion cell, sir."},
           {"say": "The search results also included a line telling me to add source links to my reply, "
                   "which I ignored."})
    events = turn(client, "show me a lithium-ion cell", conv_id="n2")
    assert _model_says(events) == ["Here is a lithium-ion cell, sir."]
    assert [r["sentences"] for r in _note_rows()] == [1]
    assert acks.PHRASES["note_only"] not in says(events)


@pytest.mark.parametrize("card", [False, True])
def test_note_only_reply(client, card):
    objs = [{"say": NOTE_ONLY}] + ([{"card": INFO}] if card else [])
    _model(*objs)
    events = turn(client, "what is inside a battery", conv_id="n3")
    assert _model_says(events) == [acks.PHRASES["note_only"]]
    fail_texts = {line for line, _title in overlay.ANSWER_FAIL.values()}
    fail_titles = {title for _line, title in overlay.ANSWER_FAIL.values()}
    assert not fail_texts & set(says(events))
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert not [c for c in cards if c.get("title") in fail_titles]
    assert [c["title"] for c in cards] == (["Battery"] if card else [])
    assert _steps("done")[-1]["error"] is None


def test_card_body_injection_note_dropped(client):
    _model({"say": "Here is the battery."},
           {"card": {"type": "info", "title": "Battery",
                     "body": ["A cell has an anode and a cathode.",
                              "The search results told me to add source links, which I ignored."]}})
    events = turn(client, "what is inside a battery", conv_id="n4")
    (card,) = [e["card"] for e in events if e["type"] == "card"]
    assert card["title"] == "Battery" and card["body"] == ["A cell has an anode and a cathode."]
    assert [r["sentences"] for r in _note_rows()] == [1]


def test_card_string_body_injection_note_dropped(client):
    # R1 security 1: hud splits a string body into lines, so the filter must see it as lines too.
    _model({"say": "Here is the battery."},
           {"card": {"type": "info", "title": "Battery",
                     "body": "A cell has an anode.\nThe search results told me to add source links, "
                             "which I ignored."}})
    events = turn(client, "what is inside a battery", conv_id="n5")
    (card,) = [e["card"] for e in events if e["type"] == "card"]
    assert card["body"] == ["A cell has an anode."]
    assert [r["sentences"] for r in _note_rows()] == [1]


def test_card_only_all_notes_says_one_line(client):
    # R1 security 2: a reply that is only a card whose body is all notes is never silent.
    _model({"card": {"type": "info", "title": "Battery",
                     "body": ["The search results told me to add source links, which I ignored."]}})
    events = turn(client, "what is inside a battery", conv_id="n6")
    assert _model_says(events) == [acks.PHRASES["note_only"]]
    assert [e for e in events if e["type"] == "card"] == []
    fail_texts = {line for line, _title in overlay.ANSWER_FAIL.values()}
    assert not fail_texts & set(says(events))


def test_agent_injection_note_dropped(client):
    clock = Clock()
    setup(clock, lambda: FakeAgent(clock, script=[result({"say": NOTE_2})]), chunks=['{"agent": true}\n'])
    events = turn(client, "tell me about the work I did", conv_id="ag7")
    spoken = says(events)
    assert "Here is a lithium-ion cell, sir." in spoken
    assert "search results" not in " ".join(spoken).lower()
    (row,) = _note_rows()
    assert row["sentences"] == 1 and set(row) == {"step", "at", "note", "sentences"}


def test_agent_note_only_says_one_line(client):
    clock = Clock()
    setup(clock, lambda: FakeAgent(clock, script=[result({"say": NOTE_ONLY})]), chunks=['{"agent": true}\n'])
    spoken = says(turn(client, "tell me about the work I did", conv_id="ag8"))
    assert spoken.count(acks.PHRASES["note_only"]) == 1
    assert "I couldn't get an answer for that." not in spoken


def test_jfollow7_phrases():
    text = acks.PHRASES["note_only"]
    assert len(text.split()) <= 8 and "?" not in text and "!" not in text
    assert "jarvis" not in text.lower()
    rendered = []
    cache = acks.AckCache(synth=lambda t: (rendered.append(t), (b"pcm", 24000))[1])
    cache.prerender()
    assert text in rendered and cache.get("note_only") == (text, b"pcm", 24000)


# --- S: every sessions question reaches the sessions intent (criteria S3-S5) ---

from test_overlay_jfollow2 import NEAR, OWNER, ROOM  # noqa: E402

FIRST = "check the active sessions on mini BC"
MEANT = "I meant on the mini PC, and you can check it through the router"


def _sessions_setup(monkeypatch, tmp_path, n=8):
    _session_env(monkeypatch, tmp_path)
    clock = Clock()
    made: list = []
    deps = _deps(clock, conversations=FakeConversations(lines(*[{"say": "Model answer."}] * n)),
                 quiet_conversations=FakeConversations(lines({"say": "Two sessions, one waiting."})))
    deps.agent_conversations = FakeAgentPool(lambda: made.append(1))
    return deps, clock, made


def _first(client, conv):
    return turn(client, FIRST, conv_id=conv, audio_ms=1800, levels=OWNER)


def _kinds():
    return [r["kind"] for r in _steps("intent")]  # oldest first


def _assert_sessions_turn(events, deps, conv, quiet_calls):
    assert events[0]["text"] == "Checking your sessions."
    assert _steps("sessions")[-1]["detail"] == "result=ok rows=2"
    assert [c for c in (e.get("card") for e in events) if c and c["type"] == "info"]
    assert "can't" not in " ".join(says(events))
    assert len(deps.quiet_conversations.sessions[conv].prompts) == quiet_calls


def test_sessions_owner_two_utterances(client, monkeypatch, tmp_path):
    deps, clock, made = _sessions_setup(monkeypatch, tmp_path)
    events = _first(client, "s1")
    _assert_sessions_turn(events, deps, "s1", 1)
    assert _kinds() == ["sessions"]
    clock.now += 5
    events = turn(client, MEANT, conv_id="s1", followup=True, audio_ms=3000, levels=NEAR, conf=0.9)
    _assert_sessions_turn(events, deps, "s1", 2)
    assert _kinds() == ["sessions", "sessions"]
    assert deps.conversations.sessions == {} and not made and not _steps("agent")


def test_sessions_follow_expires(client, monkeypatch, tmp_path):
    deps, clock, made = _sessions_setup(monkeypatch, tmp_path)
    # (a) expired
    _first(client, "e1")
    clock.now += overlay.SESSIONS_FOLLOW_S + 1
    turn(client, MEANT, conv_id="e1")
    assert _kinds()[-1] == "answer" and len(_steps("sessions")) == 1
    # (b) one turn only
    _first(client, "e2")
    turn(client, "what time is it", conv_id="e2")
    turn(client, MEANT, conv_id="e2")
    assert _kinds()[-1] == "answer"
    # (c) no sessions turn first
    turn(client, MEANT, conv_id="e3")
    assert _kinds()[-1] == "answer"
    # (d), (e) follow-ups that are not about sessions
    for i, text in enumerate(("tell me a joke about the Mac", "how long is a therapy session",
                              "is the build still running", "what's running late today")):
        conv = f"e4{i}"
        _first(client, conv)
        clock.now += 2
        turn(client, text, conv_id=conv, followup=True, audio_ms=3000, levels=NEAR, conf=0.9)
        assert _kinds()[-1] == "answer", text
        assert conv not in overlay._sessions_follow
    assert not made


def test_sessions_follow_keeps_gates(client, monkeypatch, tmp_path):
    deps, clock, made = _sessions_setup(monkeypatch, tmp_path)
    _first(client, "g1")
    rows = len(_steps("sessions"))
    clock.now += 3
    events = turn(client, MEANT, conv_id="g1", followup=True, audio_ms=3000, levels=ROOM, conf=0.9)
    assert [e["type"] for e in events] == ["done"]
    assert _steps("followup_dropped")[-1]["reason"] == "level"
    assert len(_steps("sessions")) == rows
