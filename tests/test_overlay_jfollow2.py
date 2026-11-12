"""jfollow2: the owner-words media path in tainted/agent conversations, cursor drops, debug notes.

All media lookups are fakes on media_lookup.find/find_video/search; the model is a
FakeConversations replaying canned JSON lines. No network, no real processes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from conftest import AUTH_HEADER  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import acks, gitdiff, intents, lean, media_lookup  # noqa: E402
# The agent tests' fixtures (`reset` is autouse: stores cleared, HOME = a fake folder) and fakes.
from test_overlay_agent import (BusRecorder, Clock, FakeAgent, client, push_script,  # noqa: F401
                                reset, result, setup)
from test_overlay_service import FakeConversations, make_deps, says, turn  # noqa: E402

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}

VAGUE = "can you get me something nice to look at"  # media_request() -> None: no owner-words ask
IMG = {"type": "image", "title": "Dog", "alt": "Dog 1",
       "url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/a1/Dog1.jpg/960px-Dog1.jpg"}


@pytest.fixture(autouse=True)
def off_view_said(monkeypatch):
    monkeypatch.setattr(overlay, "_off_view_said", None)  # other test files read it across tests


def _fixture():
    spec = importlib.util.spec_from_file_location(
        "hunk_fixture_jf2", Path(__file__).parent / "fixtures/overlay/hunk_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(tmp_path, mode: str = "") -> dict:
    return _fixture().build(tmp_path / "home" / "r", mode=mode)


def _deps(**kw):
    calls: list = []
    deps = make_deps(run=lambda cmd, **k: calls.append(list(cmd)) or SimpleNamespace(returncode=0, stdout=""),
                     **kw)
    overlay.set_deps(deps)
    return deps


def _on_screen(repo: dict) -> dict:
    return {"app": "Code", "document": str(Path(repo["repo"]) / "sample.py")}


def _lines(events):
    return [e for e in events if e["type"] == "lines"]


def _cards(events):
    return [e["card"] for e in events if e["type"] == "card"]


def _steps(name: str) -> list[dict]:
    turns = reversed(overlay.debug_log.snapshot()["turns"])  # oldest first
    return [s for t in turns for s in t["steps"] if s["step"] == name]


def _until(cond, n=300):
    for _ in range(n):
        if cond():
            return True
        __import__("time").sleep(0.01)
    return cond()


def lines(*objs) -> list[str]:
    return [json.dumps(o) + "\n" for o in objs]


def hit(n: int, **over) -> dict:
    return {"url": f"https://thumb.wikimedia.org/wikipedia/commons/thumb/a/a{n}/Dog{n}.jpg/960px-Dog{n}.jpg",
            "title": f"Dog {n}", "source_page": f"https://commons.wikimedia.org/wiki/File:Dog{n}.jpg",
            "licence": "CC0", "artist": f"Author {n}", **over}


class FakeFind:
    """Stands in for media_lookup.find (the owner-words image path)."""

    def __init__(self):
        self.calls = []

    def __call__(self, query, count=1):
        self.calls.append((query, count))
        return dict(IMG)


class FakeSearch:
    """Stands in for media_lookup.search (the model image path)."""

    def __init__(self, hits=()):
        self.calls, self.hits = [], list(hits)

    def __call__(self, query, count=1):
        self.calls.append((query, count))
        return self.hits[:count]


def agent_pool() -> lean.Conversations:
    return lean.Conversations(lambda: SimpleNamespace(close=lambda: None))


# --- item 1: the owner-words picture in a tainted agent conversation ------------------------


@pytest.mark.parametrize("followup", [True, False])
def test_tainted_followup_picture_uses_owner_words(client, monkeypatch, followup):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    deps = make_deps()
    deps.agent_conversations = agent_pool()
    deps.agent_conversations.get("ow1")  # a warm agent session: the conversation holds agent state
    overlay.set_deps(deps)
    overlay._tainted.add("ow1")
    events = turn(client, "show me a picture of a dog", conv_id="ow1",
                  followup=followup, audio_ms=900)
    assert find.calls == [("dog", 1)]
    assert deps.conversations.sessions == {} and deps.quiet_conversations.sessions == {}
    assert says(events) == ["Looking for a picture of a dog."]
    assert [{k: v for k, v in c.items() if k != "id"} for c in _cards(events) if c["type"] == "image"] == [IMG]
    assert _steps("media")[-1]["detail"] == "path=owner_words kind=image count=1 words=1 result=card"
    assert "ow1" in overlay._tainted
    # The wake turn (followup=False) must not have started fresh: agent state still held.
    assert overlay._holds_agent_state("ow1", deps, cursor=False)


def test_owner_words_query_never_holds_context(client, monkeypatch):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    _deps()
    overlay._tainted.add("ow2")
    overlay._history.setdefault("ow2", deque(maxlen=6)).append(("SECRET-TOKEN-123", "SECRET-TOKEN-123"))
    overlay._notes.setdefault("ow2", deque(maxlen=3)).append("SECRET-TOKEN-123")
    screen = {"app": "Code", "ocr": [{"text": "SECRET-TOKEN-123"}]}
    turn(client, "show me a picture of a dog", conv_id="ow2", screen=screen)
    assert find.calls == [("dog", 1)] and "SECRET" not in find.calls[0][0]
    turn(client, "show me a picture of SECRET-TOKEN-123", conv_id="ow2", screen=screen)
    want = intents.media_request("show me a picture of SECRET-TOKEN-123").subject
    assert find.calls[-1] == (want, 1)
    assert want == media_lookup.clean_query("SECRET-TOKEN-123").lower()
    assert set(want.split()) <= set("show me a picture of secret-token-123".split())


def test_model_media_while_tainted_still_refused(client, monkeypatch):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    # Agent-held: a warm agent session AND the taint (taint is the only quiet-pool router).
    deps = make_deps(quiet_conversations=FakeConversations(
        lines({"say": "Looking."}, {"image_search": {"query": "dog", "count": 1}})))
    deps.agent_conversations = agent_pool()
    deps.agent_conversations.get("ow3")
    overlay.set_deps(deps)
    overlay._tainted.add("ow3")
    events = turn(client, VAGUE, conv_id="ow3")
    assert find.calls == []
    assert says(events).count(overlay.AGENT_MEDIA_LINE) == 1
    assert _steps("media")[-1]["detail"] == "refused source=model reason=agent_state"

    # Screen taint only: no agent pool, so no agent state.
    deps2 = make_deps(quiet_conversations=FakeConversations(
        lines({"say": "Looking."}, {"image_search": {"query": "dog", "count": 1}})))
    overlay.set_deps(deps2)
    overlay._tainted.add("ow3b")
    events = turn(client, VAGUE, conv_id="ow3b")
    assert find.calls == []
    assert says(events).count(overlay.QUIET_MEDIA_LINE) == 1
    assert _steps("media")[-1]["detail"] == "refused source=model reason=screen_taint"


def test_media_lines_are_true():
    bad = re.compile(r"five|minute|quiet", re.I)
    for line in (overlay.QUIET_MEDIA_LINE, overlay.AGENT_MEDIA_LINE, overlay.MEDIA_HOW_LINE):
        assert "show me a picture of" in line
        assert not bad.search(line), line
    assert "five quiet" not in (ROOT / "gateway/overlay.py").read_text()


def test_no_subject_says_the_way_out(client, monkeypatch):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    _deps()
    overlay._tainted.add("ow5")
    events = turn(client, "show me a picture of that", conv_id="ow5")
    assert find.calls == []
    assert says(events) == [overlay.MEDIA_HOW_LINE]
    assert _steps("media")[-1]["detail"] == "path=owner_words result=no_subject"


def test_untainted_picture_keeps_the_model_path(client, monkeypatch):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    web = FakeConversations(lines({"say": "Looking for a picture of a dog."},
                                  {"image_search": {"query": "dog", "count": 1}}))
    deps = make_deps(conversations=web)
    overlay.set_deps(deps)
    events = turn(client, "show me a picture of a dog", conv_id="ow6")
    assert find.calls == [("dog", 1)]  # driven by the web model's image_search line
    assert not any(s.get("kind") == "media" for s in _steps("intent"))
    assert web.sessions and not deps.quiet_conversations.sessions
    assert len([c for c in _cards(events) if c["type"] == "image"]) == 1
    assert not [s for s in _steps("intent") if s.get("kind") == "media"]


def test_quiet_prompt_gives_the_reason():
    assert "could carry that text out" in overlay.QUIET_PROMPT
    assert "show me a picture of" in overlay.QUIET_PROMPT
    for bad in ("image_search", "video_search", "WebSearch"):
        assert bad not in overlay.QUIET_PROMPT
    # ANSWER_PROMPT at base 2540d60; it must not change (jfollow2 D-A4)
    assert hashlib.sha256(overlay.ANSWER_PROMPT.encode()).hexdigest() == \
        "178c8219f36672d0d5fd822cf70abea3a79513e206534c1debfd1b20eac36799"


def test_media_turn_joined_continuation_falls_back(client, monkeypatch):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    quiet = FakeConversations(lines({"say": "No."}))
    deps = make_deps(quiet_conversations=quiet)
    overlay.set_deps(deps)
    overlay._tainted.add("ow8")
    overlay._pending["ow8"] = ("show me a picture of", deps.clock())
    events = turn(client, "a dog", conv_id="ow8", followup=True)
    assert _steps("continued")  # joined=True skips the owner-words hook: the quiet model answers
    assert _steps("media") == []
    assert not [s for s in _steps("intent") if s.get("kind") == "media"]
    assert find.calls == []
    assert quiet.sessions and "No." in says(events)


def test_short_followup_picture_is_dropped_as_stray(client, monkeypatch):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    quiet = FakeConversations(lines({"say": "No."}))
    deps = make_deps(quiet_conversations=quiet)
    overlay.set_deps(deps)
    overlay._tainted.add("ow9")
    events = turn(client, "a picture please", conv_id="ow9", followup=True, audio_ms=400)
    assert _steps("followup_dropped")[-1]["detail"] == "words=3"
    assert find.calls == [] and says(events) == [] and not quiet.sessions


def test_deixis_picture_is_not_searched(client, monkeypatch):
    find = FakeFind()
    monkeypatch.setattr(media_lookup, "find", find)
    quiet = FakeConversations(lines({"say": "This page."}))
    deps = make_deps(quiet_conversations=quiet)
    overlay.set_deps(deps)
    overlay._tainted.add("ow10")
    events = turn(client, "show me a picture of this page", conv_id="ow10")
    # "this page" is deixis: the look tier owns the turn, not the owner-words media hook.
    assert _steps("intent")[-1]["kind"] == "look"
    assert find.calls == [] and _steps("media") == []
    assert "This page." in says(events)


# --- item 11-12: the git_diff repo pick and cursor drops ------------------------------------


def test_git_diff_uses_the_terminal_repo_pick(client, monkeypatch, tmp_path):
    repo = _build(tmp_path, "dirty")
    _deps()
    monkeypatch.setattr(overlay, "_terminal_cwd", lambda app, title="", info=None: Path(repo["repo"]))
    monkeypatch.delenv("OVERLAY_GIT_REPO", raising=False)
    screen = {"app": "Terminal", "windows": [{"app": "Terminal", "title": "r — -zsh — 80×24"}]}
    events = turn(client, "highlight the changes", conv_id="t11", screen=screen)
    assert _steps("repo_pick")[-1]["detail"].startswith("terminal")
    # Round 4 (hub, Mac run 2 B1): only a Terminal in front -> cards and the cards-only line, no
    # `lines` event: the app's title match would box another repo's sample.py tab over the Terminal.
    assert _lines(events) == []
    assert any(c.get("id") == "gd-list" for c in _cards(events))
    assert acks.PHRASES["cards_only"].format(name="sample.py") in says(events)[-1]
    assert _lines(turn(client, "next change", conv_id="t11")) == []  # the cursor keeps the rule

    monkeypatch.setattr(overlay, "_terminal_cwd", lambda app, title="", info=None: None)
    monkeypatch.setattr(gitdiff, "repo_for", lambda *a: None)
    events = turn(client, "highlight the changes", conv_id="t11b", screen=screen)
    assert says(events)[-1] == acks.PHRASES["no_repo"]  # after the diff ack
    src = (ROOT / "gateway/overlay.py").read_text() + (ROOT / "mac/voice/acks.py").read_text()
    assert "Set OVERLAY_GIT_REPO" not in src


def test_agent_no_change_list_drops_the_old_cursor(client, monkeypatch, tmp_path):
    repo = _build(tmp_path, "dirty")
    _deps()
    turn(client, "highlight the changes", conv_id="a12", screen=_on_screen(repo))
    assert "a12" in overlay._hunks
    clock = Clock()
    monkeypatch.setattr(overlay, "_last_change", lambda run: (None, None, None))  # no card, no nav
    setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Two commits."})]))
    turn(client, "what changed in this file", conv_id="a12", screen=_on_screen(repo))
    assert "a12" not in overlay._hunks
    events = turn(client, "next change", conv_id="a12")
    assert acks.PHRASES["no_hunks"] in says(events)
    assert not [c for c in _cards(events) if c.get("id") == "gd-list"]


def test_warm_agent_turn_without_a_file_drops_the_old_cursor(client, monkeypatch, tmp_path):
    # Mac run 2, B3: a warm "what changed in the last commit" with no file on screen (run.screen_file
    # None) left the 1/1 cursor alive, so "next change" said "That was the last change."
    repo = _build(tmp_path, "dirty")
    _deps()
    turn(client, "highlight the changes", conv_id="a13", screen=_on_screen(repo))
    assert "a13" in overlay._hunks
    clock = Clock()
    setup(clock, lambda: FakeAgent(clock, script=[result({"say": "One commit."})]))
    turn(client, "what changed in the last commit", conv_id="a13")
    assert "a13" not in overlay._hunks
    events = turn(client, "next change", conv_id="a13")
    assert acks.PHRASES["no_hunks"] in says(events)
    assert not [c for c in _cards(events) if c.get("id") == "gd-list"] and _lines(events) == []


def test_same_owner_words_card_replaces_its_twin(client, monkeypatch):
    # Mac run 2, finding 5: "show me a picture of a dog" twice left two dog cards. Same words ->
    # same card id, so the app swaps the card; other words get another id.
    monkeypatch.setattr(media_lookup, "find", FakeFind())
    _deps()
    overlay._tainted.add("ow11")
    ids = []
    for text in ("show me a picture of a dog", "Hey Jarvis, show me a picture of a dog.",
                 "show me a picture of a cat"):
        ids += [c["id"] for c in _cards(turn(client, text, conv_id="ow11")) if c["type"] == "image"]
    assert len(ids) == 3 and ids[0] == ids[1] != ids[2]


def test_git_diff_none_drops_the_cursor(client, monkeypatch, tmp_path):
    repo = _build(tmp_path, "dirty")
    _deps()
    turn(client, "highlight the changes", conv_id="n12", screen=_on_screen(repo))
    assert "n12" in overlay._hunks
    monkeypatch.setattr(gitdiff, "pick_range",
                        lambda repo_, sub="auto": SimpleNamespace(kind="none", hunks=[], base=None))
    events = turn(client, "highlight the changes", conv_id="n12", screen=_on_screen(repo))
    assert "n12" not in overlay._hunks  # the branch pops the old cursor and sets no new one
    assert acks.PHRASES["diff_none_auto"] in says(events)


# --- item 13: the short-file and folded debug notes ------------------------------------------


def test_short_file_line(client, monkeypatch):
    bus = BusRecorder()
    monkeypatch.setattr(overlay, "bus", bus)
    _deps()

    def note(detail):
        assert client.post("/overlay/debug/note", json={"step": "lines", "detail": detail},
                           headers=AUTH_HEADER).status_code == 200

    note("no_gutter win=title try=3 want=3-3")
    want = acks.PHRASES["lines_short"].format(n=3)
    assert [(k, e["text"]) for k, e in bus.events] == [("hud", want)]
    overlay._off_view_said = None
    bus.events.clear()
    note("no_gutter try=3 want=3-3 extra words")  # extra words never reach the line
    assert [(k, e["text"]) for k, e in bus.events] == [("hud", want)]
    overlay._off_view_said = None
    bus.events.clear()
    note("no_gutter win=title try=1 want=3-3")  # the whole block is gated by try=3
    assert bus.events == []


def test_box_line_has_a_spoke_row(client, monkeypatch):
    # Mac run 2, B4: the lines_short line left no trace on the debug page, so a dropped push and
    # a spoken one looked the same. Now a `spoke` row with the push note sits on the lines' turn.
    monkeypatch.setattr(overlay, "bus", BusRecorder())
    _deps()
    client.post("/overlay/debug/note", json={"step": "lines", "detail": "no_gutter win=title try=3 want=3-3"},
                headers=AUTH_HEADER)
    row = _steps("spoke")[-1]
    assert row["text"] == acks.PHRASES["lines_short"].format(n=3)
    assert row["ms"] is None and row["note"] == "pushed to the app, played when idle"
    assert [s["step"] for s in overlay.debug_log.snapshot()["turns"][0]["steps"]][-2:] == ["lines", "spoke"]


def test_folded_note_speaks(client, monkeypatch):
    bus = BusRecorder()
    monkeypatch.setattr(overlay, "bus", bus)
    _deps()
    client.post("/overlay/debug/note", json={"step": "lines", "detail": "folded seen=1-40 try=3 want=12-12"},
                headers=AUTH_HEADER)
    assert [(k, e["text"]) for k, e in bus.events] == [("hud", acks.PHRASES["lines_off_view"])]


# --- item 14: the expiry spoke row carries a note --------------------------------------------


def test_expiry_row_has_a_note(client, monkeypatch):
    monkeypatch.setattr(overlay, "bus", BusRecorder())
    clock = Clock()
    setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="x1")
    clock.now += 31
    assert _until(lambda: any(s.get("state") == "expired" for s in _steps("agent_confirm")))
    spoke = [s for s in overlay.debug_log.snapshot()["turns"][0]["steps"] if s["step"] == "spoke"]
    assert spoke and spoke[-1]["note"] == "pushed to the app, played when idle"
    html = (ROOT / "gateway/overlay_debug.html").read_text()
    assert "s.ms == null" in html
    assert "` · text at ${s.ms} ms, no audio`" in html


# --- round 6: the follow-up speech gate (room talk is not the owner) ---------------------------------

OWNER = [0.002] * 5 + [0.12] * 12 + [0.004] * 8  # wake turn: the owner near the Mac, 100 ms blocks
NEAR = [0.003] * 4 + [0.1] * 8 + [0.003] * 6  # the owner again, same distance
ROOM = [0.003] * 10 + [0.03] * 15 + [0.004] * 5  # Mac run 3 room talk: about a quarter of his level
ROOM_TEXTS = ("Right", "I", "thought", "Exactly. I had to be worried, but I had to worry. there",
              "Did you hear that one song")


@pytest.fixture(autouse=True)
def owner_levels():
    overlay._owner_level.clear()
    yield
    overlay._owner_level.clear()


def _owner_wake(client, conv: str):
    turn(client, "what's the weather like", conv_id=conv, audio_ms=1800, levels=OWNER)


def _dropped():
    return _steps("followup_dropped")[-1]


def test_room_followups_are_dropped_by_level(client):
    loud = FakeConversations(lines(*[{"say": "Answer."}] * 8))
    _deps(conversations=loud)
    _owner_wake(client, "g1")
    sessions = len(loud.sessions)
    for text in ROOM_TEXTS:
        events = turn(client, text, conv_id="g1", followup=True, audio_ms=3000, levels=ROOM, conf=0.9)
        assert [e["type"] for e in events] == ["done"], text
        row = _dropped()
        assert (row["reason"], row["level"], row["conf"]) == ("level", 0.03, 0.9), text
        assert row["detail"] == f"words={len(re.findall(r'[A-Za-z0-9]+', text))}"
    assert len(loud.sessions) == sessions  # no model call for any of them


def test_low_confidence_followup_is_dropped(client):
    loud = FakeConversations(lines(*[{"say": "Answer."}] * 4))
    _deps(conversations=loud)
    _owner_wake(client, "g2")
    events = turn(client, "Did you hear that one song", conv_id="g2", followup=True, levels=NEAR, conf=0.2)
    assert [e["type"] for e in events] == ["done"]
    assert (_dropped()["reason"], _dropped()["conf"]) == ("confidence", 0.2)


def test_short_room_fragments_are_dropped(client):
    _deps(conversations=FakeConversations(lines(*[{"say": "Answer."}] * 4)))
    _owner_wake(client, "g3")
    for text, n in (("What are you people", 4), ("Right", 1), ("thought", 1)):
        events = turn(client, text, conv_id="g3", followup=True, levels=NEAR)
        assert [e["type"] for e in events] == ["done"], text
        assert (_dropped()["reason"], _dropped()["detail"]) == ("short", f"words={n}")
    assert intents.noise_words("What are you people") == 4
    assert intents.noise_words("what about you guys?") == 4
    assert intents.noise_words("show me pictures of guys") is None


def test_owner_followups_at_near_field_are_answered(client):
    loud = FakeConversations(lines(*[{"say": "Answer."}] * 6))
    _deps(conversations=loud)
    _owner_wake(client, "g4")
    for text in ("what about tomorrow", "Did you hear that one song", "and the second one is what"):
        events = turn(client, text, conv_id="g4", followup=True, audio_ms=1500, levels=NEAR, conf=0.8)
        assert "Answer." in says(events), text
    assert not _steps("followup_dropped")
    # No owner level yet (first follow-up after a typed turn): only the absolute floor applies.
    events = turn(client, "what about tomorrow", conv_id="g5", followup=True, levels=NEAR)
    assert "Answer." in says(events)
    # An app without levels or confidence: the old rules only.
    events = turn(client, "what about tomorrow", conv_id="g4", followup=True)
    assert "Answer." in says(events)


def test_known_commands_are_never_dropped(client, tmp_path):
    repo = _build(tmp_path)
    _deps(conversations=FakeConversations(lines(*[{"say": "Answer."}] * 4)))
    _owner_wake(client, "g6")
    quiet = {"followup": True, "levels": ROOM, "conf": 0.1}
    turn(client, "highlight the changes", conv_id="g6", screen=_on_screen(repo))
    events = turn(client, "next change", conv_id="g6", **quiet)
    assert _lines(events) or any(s.get("kind") == "hunk" for s in _steps("intent"))
    events = turn(client, "clear", conv_id="g6", **quiet)
    assert {"type": "clear"} in events
    for text in ("stop", "yes", "no"):
        turn(client, text, conv_id="g6", **quiet)
    assert not _steps("followup_dropped")


def test_confirm_answer_with_room_level_is_not_dropped(client):
    clock = Clock()
    setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="g7", levels=OWNER, audio_ms=1500)
    events = turn(client, "yes", conv_id="g7", followup=True, levels=ROOM, conf=0.1)
    assert "Pushed." in says(events)
    assert not _steps("followup_dropped")


def test_wake_turn_is_never_level_gated(client):
    _deps(conversations=FakeConversations(lines({"say": "Answer."})))
    events = turn(client, "Did you hear that one song", conv_id="g8", audio_ms=1500, levels=ROOM, conf=0.1)
    assert "Answer." in says(events) and not _steps("followup_dropped")


def test_room_followup_does_not_join_a_pending_head(client):
    _deps(conversations=FakeConversations(lines({"say": "Answer."})))
    _owner_wake(client, "g9")
    turn(client, "show me the last, uh", conv_id="g9", followup=True, levels=NEAR)
    assert "g9" in overlay._pending
    events = turn(client, "Exactly. I had to be worried", conv_id="g9", followup=True, levels=ROOM)
    assert [e["type"] for e in events] == ["done"] and not _steps("continued")
    assert "g9" in overlay._pending  # the owner's own rest can still join


def test_speech_gate_is_tunable_and_shown(client, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SOME_TOKEN=x\nOVERLAY_FOLLOWUP_MIN_CONF='0.6'\n")
    monkeypatch.setattr(overlay, "_ENV_PATH", env)
    monkeypatch.delenv("OVERLAY_FOLLOWUP_MIN_CONF", raising=False)
    assert overlay._tunable("OVERLAY_FOLLOWUP_MIN_CONF", 0.35) == 0.6
    monkeypatch.setenv("OVERLAY_FOLLOWUP_MIN_CONF", "0.5")
    assert overlay._tunable("OVERLAY_FOLLOWUP_MIN_CONF", 0.35) == 0.5
    monkeypatch.setenv("OVERLAY_FOLLOWUP_MIN_CONF", "loud")
    assert overlay._tunable("OVERLAY_FOLLOWUP_MIN_CONF", 0.35) == 0.35
    assert "SOME_TOKEN" not in os.environ
    _deps()
    gate = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["speech_gate"]
    assert gate == {"followup_level_ratio": 0.5, "followup_min_level": 0.01, "followup_min_voiced_ms": 300,
                    "followup_min_conf": 0.35, "tail_level_ratio": 0.5}
    assert client.get("/overlay/health", headers=AUTH_HEADER).json()["tail_level_ratio"] == 0.5
    page = overlay.DEBUG_PAGE
    for name in ("OVERLAY_FOLLOWUP_LEVEL_RATIO", "OVERLAY_FOLLOWUP_MIN_LEVEL", "OVERLAY_FOLLOWUP_MIN_VOICED_MS",
                 "OVERLAY_FOLLOWUP_MIN_CONF", "OVERLAY_TAIL_LEVEL_RATIO", 'case "followup_dropped"'):
        assert name in page, name
    for bad in ({"text": "x", "levels": [0.1] * 401}, {"text": "x", "conf": 1.5}):
        assert client.post("/overlay/utterance", json=bad, headers=AUTH_HEADER).status_code == 422


# Round 9 (security): the app's TailGate must never cut a wake turn down to a yes/no/cancel the
# whole utterance was not. Its answer vocabulary has to cover every word the service accepts.
def _answer_vocabulary() -> set[str]:
    pats = (intents._POLITE, intents._CONFIRM_RE.pattern, intents._CANCEL_RE.pattern, intents._NO_RE.pattern)
    words: set[str] = set()
    for p in pats:
        # A glued optional suffix ("confirm(?:ed)?") gives the stem and the long word, never "ed".
        words |= {a + b for a, b in re.findall(r"([a-z]+)\(\?:([a-z]+)\)\?", p)}
        p = re.sub(r"(?<=[a-z])\(\?:[a-z]+\)\?", "", p)
        words |= {w.replace("'", "") for w in re.findall(r"[a-z']+", p)}
    return words | {"nevermind"}  # "never ?mind" also matches as one word


def _swift_tail_gate() -> str:
    return (ROOT / "mac/overlay/Sources/OverlayCore/TurnEndpointer.swift").read_text()


def test_tail_gate_answer_words_cover_the_service_vocabulary():
    src = _swift_tail_gate()
    m = re.search(r"static let answerWords: Set<String> = \[(.*?)\]", src, re.S)
    assert m, "TailGate.answerWords not found"
    swift = set(re.findall(r'"([a-z]+)"', m.group(1)))
    missing = _answer_vocabulary() - swift
    assert not missing, f"words the service accepts as an answer but TailGate would cut: {sorted(missing)}"
    for phrase in ("yes thank you", "confirmed thanks", "no thanks", "never mind please", "that's enough sir"):
        assert intents.is_confirm(phrase) or intents.is_no(phrase) or intents.is_cancel(phrase)
        assert {w.replace("'", "") for w in phrase.split()} <= swift


def test_tail_gate_strips_the_same_misheard_wake():
    # "Hey, John Pierce. Yes, thanks." is a yes to the service (_MISHEARD_WAKE_RE drops the first
    # sentence), so the app must drop it the same way before its answer check.
    assert intents.is_confirm("Hey, John Pierce. Yes, thanks.")
    assert intents._MISHEARD_WAKE_RE.pattern in _swift_tail_gate()
