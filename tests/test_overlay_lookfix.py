"""f52-lookfix: own-card line only when nothing on screen matches, a conversation life cycle so
pictures work again after a screen question, and small look-path fixes. Fakes only: no model,
no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from gateway import overlay  # noqa: E402
from test_overlay_seescreen import (  # noqa: E402,F401  (shared stubs + fixtures)
    ReplyConversations, client, fields, ground_steps, id_of, make_deps, reset_overlay_state, says,
    screen, shapes, turn,
)


@pytest.fixture(autouse=True)
def reset_lookfix_state():
    stores = (overlay._card_text, overlay._last_reply, overlay._pending, overlay._confirm,
              overlay._expired, overlay._agent_busy)
    for store in stores:
        store.clear()
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
