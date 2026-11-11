"""f52-lookfix: own-card line only when nothing on screen matches, a conversation life cycle so
pictures work again after a screen question, and small look-path fixes. Fakes only: no model,
no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from conftest import AUTH_HEADER  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import lean, media_lookup  # noqa: E402
from test_overlay_seescreen import (  # noqa: E402,F401  (shared stubs + fixtures)
    FakeConversations, ReplyConversations, client, fields, ground_steps, id_of, make_deps,
    reset_overlay_state, says, screen, shapes, turn,
)


@pytest.fixture(autouse=True)
def reset_lookfix_state():
    stores = (overlay._card_text, overlay._last_reply, overlay._resets, overlay._pending, overlay._confirm,
              overlay._expired, overlay._agent_busy, overlay._notes, overlay._card_ids)
    for store in stores:
        store.clear()
    overlay.debug_log.clear()
    yield
    for store in stores:
        store.clear()


# --- D7: ids and labels the look fallback points at ---------------------------------------------

TARGETS = [{"id": 1, "source": "ocr", "role": "text", "label": "Stats", "box": [0, 0, 10, 10]},
           {"id": 2, "source": "ocr", "role": "text", "label": 'Total  "tokens" <b>', "box": [0, 20, 10, 30]}]


def test_point_digits():
    assert overlay._point_at("²", TARGETS) is None  # "²".isdigit() is True; int("²") used to raise
    assert overlay._point_at("1" * 30, TARGETS) is None
    assert overlay._point_at("٣", TARGETS) is None  # non-ASCII digits are text, and too short
    hit, via = overlay._point_at(2.0, TARGETS)
    assert (hit["id"], via) == (2, "point_id")
    assert overlay._point_at(2.5, TARGETS) is None and overlay._point_at(True, TARGETS) is None


def test_point_safe_label():
    hit, via = overlay._point_at("total tokens b", TARGETS)  # the label as the model saw it
    assert (hit["id"], via) == (2, "point_text")


# --- D1-D3: history only on reference words; an on-screen match beats Jarvis's own card --------

PAGE = ["Saudi Arabia", "Flag", "Emblem", "Motto: There is no god but God", "Capital: Riyadh"]
ASK_MOTTO = "what is the motto on this page?"
MOTTO = ['{"say": "The motto is the Shahada."}',
         '{"card": {"type": "info", "title": "Motto of Saudi Arabia", "body": ["There is no god but God"]}}']
ASK_JAPAN = "what is the population of Japan? put it on a card."
JAPAN = ['{"say": "About 123.7 million."}',
         '{"card": {"type": "stat", "title": "Population of Japan", "value": "about 123.7 million",'
         ' "label": "2024 estimate"}}']


class RecVLM:
    """Records every grounding prompt; picks the candidate labelled `pick`, else null."""

    def __init__(self, pick=None):
        self.pick, self.prompts = pick, []

    def __call__(self, prompt, jpeg):
        self.prompts.append(prompt)
        return '{"id": %s}' % (id_of(prompt, self.pick) if self.pick else "null")


def look(client, ask, first, ask2, fallback=lambda p: [], vlm=None, conv="l1", texts=PAGE):
    """Turn 1 `ask` answers with `first`; turn 2 (follow-up) `ask2` looks; its fallback replies
    `fallback(prompt)`. Returns (turn-2 events, vlm, model calls made by turn 2: (vlm, answer))."""
    vlm = vlm or RecVLM()

    def reply(prompt):
        return [line + "\n" for line in (fallback(prompt) if prompt.startswith(ask2) else first)]
    pool = ReplyConversations(reply)
    overlay.set_deps(make_deps(vlm=vlm, conversations=pool, quiet_conversations=pool))
    turn(client, ask, conv_id=conv, screen=screen(texts))
    vlm_before, answer_before = len(vlm.prompts), sum(len(s.prompts) for s in pool.sessions.values())
    events = turn(client, ask2, conv_id=conv, followup=True, screen=screen(texts))
    calls = (len(vlm.prompts) - vlm_before, sum(len(s.prompts) for s in pool.sessions.values()) - answer_before)
    return events, vlm, calls


def ground_row(client) -> dict:
    return fields(ground_steps(client)[0]["detail"])


def label_of(event) -> str:
    return event["shape"]["label"]


def test_named_no_history(client):
    events, vlm, calls = look(client, ASK_MOTTO, MOTTO, "highlight the title", vlm=RecVLM(pick="Saudi Arabia"))
    assert "<history" not in vlm.prompts[-1] and "Shahada" not in vlm.prompts[-1]
    assert ground_row(client)["history"] == "0"
    assert [label_of(e) for e in shapes(events)] == ["Saudi Arabia"]
    assert says(events)[-1] == "Here." and overlay.OWN_CARD_LINE not in says(events)
    assert calls == (1, 0)  # base 34e2ce5: 1 VLM call, no answer call on a hit


@pytest.mark.parametrize("ask2", ["can you highlight it for me", "highlight this page's title",
                                  "highlight the second one"])
def test_reference_history(client, ask2):
    _events, vlm, _calls = look(client, ASK_MOTTO, MOTTO, ask2)
    assert '<history untrusted="true">' in vlm.prompts[-1]
    assert int(ground_row(client)["history"]) > 0


def test_onscreen_wins_calls(client):
    """Live (b): VLM null, the fallback points at the card's title, which holds the page title."""
    events, _vlm, calls = look(client, ASK_MOTTO, MOTTO, "highlight the title",
                               fallback=lambda p: ['{"point": "Motto of Saudi Arabia"}'])
    assert [label_of(e) for e in shapes(events)] == ["Saudi Arabia"]
    assert overlay.OWN_CARD_LINE not in says(events) and says(events)[-1] == "Here."
    assert ground_row(client)["via"] == "point_text"
    assert calls == (1, 1)  # base: the same two calls (it then said the own-card line)


def test_onscreen_longest_label_wins():
    targets = [{"id": 1, "source": "ocr", "role": "text", "label": "Saudi", "box": [0, 0, 1, 1]},
               {"id": 2, "source": "ocr", "role": "text", "label": "Saudi Arabia", "box": [0, 2, 1, 3]},
               {"id": 3, "source": "ocr", "role": "text", "label": "Arabia", "box": [0, 4, 1, 5]},
               {"id": 4, "source": "ocr", "role": "text", "label": "Flag", "box": [0, 6, 1, 7]}]
    hit, via = overlay._label_inside("Motto of Saudi Arabia", targets)
    assert (hit["id"], via) == (2, "point_text")
    assert overlay._label_inside("the flag", targets) is None  # "flag" is under 5 chars
    tie = [dict(targets[0], id=9), dict(targets[0], label="Riyad", id=5)]
    assert overlay._label_inside("Saudi Riyad", tie)[0]["id"] == 5  # same length: lowest id
    assert overlay._label_inside(None, targets) is None and overlay._label_inside(7, targets) is None


def test_onscreen_match_only_instead_of_own_card(client):
    """No card holds the point: a label inside it is not drawn, the plain no-pin line stays."""
    first = ['{"say": "The motto is the Shahada."}']
    events, _vlm, _calls = look(client, ASK_MOTTO, first, "highlight the title",
                                fallback=lambda p: ['{"point": "Motto of Saudi Arabia"}'])
    assert shapes(events) == [] and says(events)[-1] == overlay.NO_PIN_LINE


def test_own_card_named_calls(client):
    events, _vlm, calls = look(client, ASK_JAPAN, JAPAN, "highlight the population of Japan",
                               fallback=lambda p: ['{"point": "Population of Japan"}'])
    assert shapes(events) == [] and says(events).count(overlay.OWN_CARD_LINE) == 1
    assert calls == (1, 1)


@pytest.mark.parametrize("point", ["null", '"123 million"'])
def test_own_card_reference_calls(client, point):
    events, _vlm, calls = look(client, ASK_JAPAN, JAPAN, "highlight it",
                               fallback=lambda p: ['{"point": %s}' % point])
    assert shapes(events) == [] and says(events).count(overlay.OWN_CARD_LINE) == 1
    assert calls == (1, 1)


def test_plain_miss(client):
    first = ['{"say": "The motto is the Shahada."}']  # no card in this conversation
    events, _vlm, _calls = look(client, ASK_MOTTO, first, "highlight it", fallback=lambda p: ['{"point": null}'])
    assert says(events).count("I can't see it on screen.") == 1 and overlay.OWN_CARD_LINE not in says(events)
    events, _vlm, _calls = look(client, ASK_MOTTO, first, "highlight it", conv="l2",
                                fallback=lambda p: ['{"point": "weather forecast"}'])
    assert says(events).count(overlay.NO_PIN_LINE) == 1 and overlay.OWN_CARD_LINE not in says(events)


def test_named_target_after_card_is_not_own_card(client):
    """A named target missing from the screen and from the card: the plain line, not the card line."""
    events, _vlm, _calls = look(client, ASK_JAPAN, JAPAN, "highlight the weather forecast",
                                fallback=lambda p: ['{"point": null}'])
    assert says(events)[-1] == "I can't see it on screen." and overlay.OWN_CARD_LINE not in says(events)


# --- D4-D5: a spoken wake word after 90 s or "clear" starts the conversation afresh -------------

DOG = "show me a picture of a dog"
OCR_WORDS = ("Motto: There is no god but God", "Capital: Riyadh", "Shahada")
HIT = {"url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/a1/Dog1.jpg/960px-Dog1.jpg",
       "title": "Dog 1", "source_page": "https://commons.wikimedia.org/wiki/File:Dog1.jpg",
       "licence": "CC0", "artist": "Author 1"}
SPOKEN = {"followup": False, "audio_ms": 900}  # a wake-word turn as the Mac app sends it


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Pool(ReplyConversations):
    """Replies by prompt; logs get/drop in one shared order list, and whether the conv was tainted at get."""

    def __init__(self, name, reply, log):
        super().__init__(lambda prompt: [line + "\n" for line in reply(prompt)])
        self.name, self.log, self.tainted_at_get = name, log, []

    def get(self, conv_id):
        self.log.append((self.name, "get", conv_id))
        self.tainted_at_get.append(conv_id in overlay._tainted)
        return super().get(conv_id)

    def drop(self, conv_id):
        self.log.append((self.name, "drop", conv_id))
        super().drop(conv_id)

    def prompts(self):
        return [p for s in self.sessions.values() for p in s.prompts]


def answers(prompt, media='{"image_search": {"query": "dog", "count": 1}}'):
    if prompt.startswith("show me a"):
        return [media]
    if prompt.startswith("what is the capital"):
        return ['{"say": "Paris."}']
    return MOTTO


def life(monkeypatch, quiet_media='{"image_search": {"query": "dog", "count": 1}}'):
    """Web and quiet pools, a fake clock and a fake picture search."""
    monkeypatch.setattr(media_lookup, "search", lambda query, count=1: [HIT])
    log: list = []
    web = Pool("web", lambda p: answers(p, '{"say": "Looking for a picture of a dog."}\n'
                                           '{"image_search": {"query": "dog", "count": 1}}'), log)
    quiet = Pool("quiet", lambda p: answers(p, quiet_media), log)
    deps = make_deps(conversations=web, quiet_conversations=quiet)
    deps.clock = Clock()
    overlay.set_deps(deps)
    return deps, web, quiet, log


def screen_question(client, conv, **extra):
    events = turn(client, ASK_MOTTO, conv_id=conv, screen=screen(PAGE), **extra)
    assert conv in overlay._tainted
    return events


def media_cards(events):
    return [e for e in events if e["type"] == "card" and e["card"]["type"] in ("image", "video")]


def steps_named(client, name):
    turns = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
    return [s for t in turns for s in t["steps"] if s["step"] == name]


def test_fresh_wake(client, monkeypatch):
    deps, web, quiet, log = life(monkeypatch)
    screen_question(client, "w1", **SPOKEN)
    deps.clock.t += 91
    events = turn(client, DOG, conv_id="w1", **SPOKEN)
    assert len(media_cards(events)) == 1 and overlay.QUIET_MEDIA_LINE not in says(events)
    [prompt] = web.prompts()
    assert "Earlier in this conversation" not in prompt
    assert not any(w in prompt for w in OCR_WORDS + (ASK_MOTTO,))
    assert web.tainted_at_get == [False]  # the reset came before the answer picked its pool
    assert log.index(("quiet", "drop", "w1")) < log.index(("web", "get", "w1"))  # old CLI gone first
    assert "w1" not in overlay._tainted


def test_fresh_wake_kept(client, monkeypatch):
    """89 s: still the same conversation, so "Jarvis, highlight it" right after a question resolves."""
    deps, web, quiet, _log = life(monkeypatch)
    screen_question(client, "w2", **SPOKEN)
    overlay.debug_log.clear()
    deps.clock.t += 89
    events = turn(client, DOG, conv_id="w2", **SPOKEN)
    assert media_cards(events) == [] and says(events).count(overlay.QUIET_MEDIA_LINE) == 1
    assert web.sessions == {} and "Earlier in this conversation" in quiet.prompts()[-1]
    assert "w2" in overlay._tainted and steps_named(client, "conversation") == []


def test_fresh_clear(client, monkeypatch):
    _deps, web, _quiet, _log = life(monkeypatch)
    screen_question(client, "c1")
    events = turn(client, "clear", conv_id="c1", followup=True)
    assert [e for e in events if e["type"] == "clear"] == [{"type": "clear"}]
    events = turn(client, DOG, conv_id="c1", followup=True)
    assert len(media_cards(events)) == 1 and overlay.QUIET_MEDIA_LINE not in says(events)
    assert not any(w in web.prompts()[-1] for w in OCR_WORDS + (ASK_MOTTO,))


@pytest.mark.parametrize("media", ['{"image_search": {"query": "dog"}}', '{"video_search": {"query": "dog"}}',
                                   '{"media": true}'])
def test_followup_keeps(client, monkeypatch, media):
    _deps, web, quiet, _log = life(monkeypatch, quiet_media=media)
    screen_question(client, "k1", **SPOKEN)
    events = turn(client, DOG.replace("picture", "video") if "video" in media else DOG, conv_id="k1",
                  followup=True, audio_ms=900)
    assert media_cards(events) == [] and says(events).count(overlay.QUIET_MEDIA_LINE) == 1
    assert "Something went wrong on my side." not in says(events)  # a directive-only reply is an answer
    assert web.sessions == {} and ASK_MOTTO in quiet.prompts()[-1]
    assert "k1" in overlay._tainted


def test_typed(client, monkeypatch):
    deps, _web, quiet, _log = life(monkeypatch)
    screen_question(client, "t1")
    deps.clock.t += 1000
    turn(client, DOG, conv_id="t1")  # followup False, no audio_ms: the debug page or a test
    assert "Earlier in this conversation" in quiet.prompts()[-1] and "t1" in overlay._tainted


SEEDS = ("SEED-HISTORY-1", "SEED-NOTE-2", "seed-card-3", "SEED-PENDING-4")


def seed(conv, deps):
    from collections import deque
    overlay._history[conv] = deque([(SEEDS[0], "Jarvis said " + SEEDS[0])], maxlen=6)
    overlay._notes[conv] = deque([SEEDS[1]], maxlen=3)
    overlay._card_text[conv] = deque([SEEDS[2]], maxlen=3)
    overlay._pending[conv] = (SEEDS[3], deps.clock())
    overlay._card_ids[conv] = {"card1"}
    overlay._last_reply[conv] = (deps.clock(), True)
    overlay._tainted.add(conv)
    deps.conversations.get(conv)
    deps.quiet_conversations.get(conv)


def test_clearing(client, monkeypatch):
    deps, web, quiet, log = life(monkeypatch)
    seed("s1", deps)
    deps.clock.t += 91
    turn(client, "what is the capital of France", conv_id="s1", **SPOKEN)
    assert ("web", "drop", "s1") in log and ("quiet", "drop", "s1") in log
    assert [u for u, _ in overlay._history["s1"]] == ["what is the capital of France"]
    for store in (overlay._notes, overlay._card_text, overlay._pending):
        assert "s1" not in store
    assert "s1" not in overlay._tainted and overlay._card_ids["s1"] == {"card1"} and overlay._confirm == {}
    vlm = RecVLM()
    deps.vlm_ask = vlm
    turn(client, "highlight it", conv_id="s1", followup=True, screen=screen(PAGE))
    prompts = vlm.prompts + web.prompts() + quiet.prompts()
    assert len(vlm.prompts) == 1 and len(prompts) >= 3
    assert not any(w.lower() in p.lower() for w in SEEDS for p in prompts)


def agent_run(conv, deps):
    session = SimpleNamespace(closed=False, respond=lambda *a, **k: None)
    return overlay.AgentRun(conv_id=conv, session=session, ctx=None, repo=None, started=deps.clock(),
                            deadline=deps.clock() + 30)


GUARDS = {
    "confirm": lambda conv, deps: overlay._confirm.__setitem__(conv, agent_run(conv, deps)),
    "expired": lambda conv, deps: overlay._expired.__setitem__(conv, (deps.clock(), "Push the branch")),
    "busy": lambda conv, deps: overlay._agent_busy.__setitem__(conv, agent_run(conv, deps)),
    "session": lambda conv, deps: deps.agent_conversations.get(conv),
}


@pytest.mark.parametrize("guard", sorted(GUARDS))
@pytest.mark.parametrize("ask", [DOG, "clear"])
def test_clearing_guarded(client, monkeypatch, guard, ask):
    """Agent-tier state for the conversation: no fresh start on a wake word or "clear", taint kept."""
    deps, _web, quiet, log = life(monkeypatch)
    deps.agent_conversations = lean.Conversations(lambda: SimpleNamespace(close=lambda: None))
    screen_question(client, "g1", **SPOKEN)
    log.clear()
    overlay.debug_log.clear()
    GUARDS[guard]("g1", deps)
    deps.clock.t += 91
    events = turn(client, ask, conv_id="g1", **SPOKEN)
    assert ("quiet", "drop", "g1") not in log and "g1" in overlay._tainted
    assert any(u == ASK_MOTTO for u, _ in overlay._history["g1"])
    assert media_cards(events) == []
    if ask == DOG and guard != "busy":  # busy: the preflight says the agent is busy and ends the turn
        assert says(events).count(overlay.QUIET_MEDIA_LINE) == 1
    if guard == "confirm":  # a pending yes is answered out loud (not silently dropped) by the preflight
        assert any(e["type"] == "card" and e["card"]["title"] == "Not done" for e in events)
    assert steps_named(client, "conversation") == []


def test_fresh_clear_after_confirm(client, monkeypatch):
    """Once the agent state is gone (the owner said no), the next "clear" starts afresh."""
    deps, _web, _quiet, _log = life(monkeypatch)
    screen_question(client, "a1")
    overlay._confirm["a1"] = agent_run("a1", deps)
    turn(client, "no", conv_id="a1", followup=True)
    assert "a1" not in overlay._confirm and "a1" in overlay._tainted
    turn(client, "clear", conv_id="a1", followup=True)
    events = turn(client, DOG, conv_id="a1", followup=True)
    assert len(media_cards(events)) == 1 and "a1" not in overlay._tainted


def test_debug_fresh(client, monkeypatch):
    deps, _web, _quiet, _log = life(monkeypatch)
    screen_question(client, "d1")
    deps.clock.t += 91
    turn(client, "what is the capital of France", conv_id="d1", **SPOKEN)
    turn(client, "clear", conv_id="d1", followup=True)
    got = steps_named(client, "conversation")
    assert [s["detail"] for s in got] == ["fresh: clear", "fresh: wake"]  # newest turn first
    assert all(set(s) == {"step", "at", "detail"} for s in got)


def test_reset_mid_turn_keeps_taint(client, monkeypatch):
    """Another turn starts the conversation afresh while a screen answer streams: that answer's
    words land in history afterwards, so the conversation must stay on the tool-less pool."""
    deps, _web, quiet, _log = life(monkeypatch)
    other_turn_reset = overlay._fresh_conversation

    def reply(prompt):
        if prompt.startswith(ASK_MOTTO.rstrip("?")):  # the intent drops the question mark
            other_turn_reset("r1", deps)  # the other turn's fresh start, mid-stream
        return answers(prompt)
    quiet.reply = lambda prompt: [line + "\n" for line in reply(prompt)]
    turn(client, ASK_MOTTO, conv_id="r1", screen=screen(PAGE))
    assert "r1" in overlay._tainted
    events = turn(client, DOG, conv_id="r1", followup=True)
    assert media_cards(events) == [] and says(events).count(overlay.QUIET_MEDIA_LINE) == 1


def test_quiet_prompt():
    assert "say so plainly" not in overlay.QUIET_PROMPT and '{"media": true}' in overlay.QUIET_PROMPT
    assert not any(w in overlay.QUIET_PROMPT for w in ("WebSearch", "WebFetch"))
    line = overlay.QUIET_MEDIA_LINE
    assert len(line) <= 80 and "clear" in line.lower() and line.count(".") == 2


# --- D6: the dropped-directive line and the look-miss line are both spoken ---------------------

def test_miss_and_drop_look_on_step(client):
    events, _vlm, _calls = look(client, ASK_MOTTO, MOTTO, "highlight the weather forecast",
                                fallback=lambda p: ['{"layout": "hide"}', '{"point": null}'])
    spoken = says(events)
    assert shapes(events) == [] and spoken.count(overlay.OVERLAY_UNCHANGED_LINE) == 1
    assert spoken.count("I can't see it on screen.") == 1
    assert spoken.index(overlay.OVERLAY_UNCHANGED_LINE) < spoken.index("I can't see it on screen.")
    assert events[-1]["type"] == "done" and [e["type"] for e in events if e["type"] in ("layout", "clear")] == []
    dropped = [s for s in steps_named(client, "layout") if s.get("state") == "dropped"]
    assert [(s["mode"], s["state"]) for s in dropped] == [("hide", "dropped")]  # recorded through on_step


def test_miss_line_never_repeats_after_a_spoken_answer(client):
    events, _vlm, _calls = look(client, ASK_MOTTO, MOTTO, "highlight the weather forecast",
                                fallback=lambda p: ['{"say": "I don\'t see a forecast."}', '{"layout": "hide"}',
                                                    '{"point": null}'])
    spoken = says(events)
    assert "I can't see it on screen." not in spoken and spoken.count(overlay.OVERLAY_UNCHANGED_LINE) == 1
