"""jfollow10c: "draw me X" (one tool-less model call -> sanitised SVG in the drawing web card) and
chart cards. The model is always a fake; no network."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import TOKEN  # noqa: E402
from common import hud, svgsafe  # noqa: E402
from gateway import overlay, overlay_log  # noqa: E402
from mac.voice import acks, intents, lean  # noqa: E402
from test_overlay_service import FakeConversations, make_app, make_deps, says, turn  # noqa: E402

DRAWINGS = ROOT / "tests" / "fixtures" / "drawings"
PHONE = (DRAWINGS / "phone.svg").read_text()
LEGEND = ["1 Display: touchscreen", "2 Side button: sleep and wake", "3 USB-C port: charging and data"]
REPLY = json.dumps({"say": "Here is an iPhone with its outside parts numbered.", "title": "iPhone parts",
                    "legend": LEGEND, "svg": PHONE})
QUIET = [0.002] * 30
ROW_RE = re.compile(r"^([a-z_]+=[A-Za-z0-9_.-]+)( [a-z_]+=[A-Za-z0-9_.-]+)*$")
DRAW_ROW = re.compile(r"^reason=ok ms=\d+ bytes_in=\d+ bytes_out=\d+ kept=\d+ removed=\d+ attrs_removed=\d+ legend=\d+$")
FAIL = "I couldn't draw that cleanly, sir."
PIE = {"type": "chart", "title": "Monthly spend", "chart": "pie", "labels": ["Rent", "Food", "Travel"],
       "series": [{"name": "Spend", "values": [1200, 400, 150]}]}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(overlay, "FEEDBACK_S", 5.0)
    for store in (overlay._history, overlay._notes, overlay._tainted, overlay._auto_tainted, overlay._card_ids,
                  overlay._pending, overlay._card_text, overlay._session_calls, overlay._owner_level,
                  overlay._card_index, overlay._card_sets):
        store.clear()
    overlay.set_deps(None)
    overlay.debug_log.clear()
    yield
    overlay.set_deps(None)


@pytest.fixture
def client(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(overlay, "TOKEN_FILE", token_file)
    return TestClient(make_app(), client=("127.0.0.1", 50000))


class FakeDraw:
    def __init__(self, reply=REPLY, exc=None):
        self.reply, self.exc, self.prompts = reply, exc, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return self.reply


def deps_with(chunks=(), draw=None, **kw):
    d = make_deps(chunks=chunks, apps={}, **kw)
    d.draw_ask = draw
    overlay.set_deps(d)
    return d


def lines(*objs):
    return [json.dumps(o) + "\n" for o in objs]


def cards(events):
    return [e["card"] for e in events if e["type"] == "card"]


def steps(name):
    return [s for t in reversed(overlay.debug_log.snapshot()["turns"]) for s in t["steps"] if s["step"] == name]


def model_asks(d):
    return sum(len(s.prompts) for pool in (d.conversations, d.quiet_conversations) for s in pool.sessions.values())


# --- ruling 1: the draw intent and the one model call ------------------------------------------
def test_draw_intent():
    draws = {
        "Hey Jarvis, draw an iPhone and label every external part and what it does":
            "an iPhone and label every external part and what it does",
        "draw me a picture of an iPhone": "an iPhone",
        "draw a simple diagram of a bicycle and label its main parts": "a bicycle and label its main parts",
        "sketch the water cycle": "the water cycle",
        "illustrate how a heart pumps blood": "how a heart pumps blood",
        "make me a labelled diagram of a volcano": "a volcano",
        "could you diagram a four-stroke engine": "a four-stroke engine",
        "draw that": "",
    }
    for text, subject in draws.items():
        assert intents.parse(text, apps={}) == intents.Intent("draw", subject), text
    for text in ("show me a pie chart of 30, 50 and 20", "show that as a line graph",
                 "draw me a pie chart of 1, 2 and 3", "what's a diagram"):
        assert intents.parse(text, apps={}).kind == "answer", text
    # "show me a diagram of X" stays the picture search it was at base (Hub question 4)
    assert intents.parse("show me a diagram of a jet engine", apps={}).kind == "answer"
    assert intents.media_request("show me a diagram of a jet engine", loose=True) is not None
    assert intents.parse("remove the chart", apps={}).kind == "card_remove"
    assert intents.draw_request("draw " + "x" * 400) is None


def test_draw_turn(client):
    draw = FakeDraw()
    d = deps_with(draw=draw)
    events = turn(client, "draw me a picture of an iPhone", conv_id="d1")
    assert says(events)[0] == acks.PHRASES["draw_started"]
    first_card = next(i for i, e in enumerate(events) if e["type"] == "card")
    assert events.index(next(e for e in events if e["type"] == "say")) < first_card
    assert "Here is an iPhone with its outside parts numbered." in says(events)
    (card,) = cards(events)
    assert card["type"] == "web" and card["page"] == "drawing" and len(card["data"]["legend"]) >= 3
    assert card["data"]["svg"] == svgsafe.sanitize(PHONE).svg
    assert draw.prompts == ["<request>an iPhone</request>"]
    assert DRAW_ROW.fullmatch(steps("draw")[-1]["detail"]), steps("draw")[-1]["detail"]
    assert model_asks(d) == 0
    # a tainted conv: still a drawing, never a picture search, no answer-pool call
    overlay._tainted.add("d2")
    events = turn(client, "draw me a picture of an iPhone", conv_id="d2")
    assert [c["page"] for c in cards(events)] == ["drawing"]
    assert not steps("media") and model_asks(d) == 0
    # no model say -> the fixed done line
    deps_with(draw=FakeDraw(json.dumps({"title": "T", "legend": [], "svg": PHONE})))
    assert acks.PHRASES["draw_done"] in says(turn(client, "sketch a phone", conv_id="d3"))


def test_draw_prompt_is_owner_words_only(client):
    draw = FakeDraw()
    deps_with(draw=draw)
    overlay._history["p1"] = deque([("what is on my screen", "SECRET-HISTORY")], maxlen=6)
    overlay._card_text["p1"] = deque(["secret-card words SECRET-CARD"], maxlen=3)
    overlay._notes["p1"] = "SECRET-SCREEN window titles"
    turn(client, "sketch the water cycle", conv_id="p1")
    assert draw.prompts == [overlay.draw_request_text("the water cycle")] == ["<request>the water cycle</request>"]
    for secret in ("SECRET-HISTORY", "SECRET-CARD", "SECRET-SCREEN", "screen"):
        assert secret not in draw.prompts[0]
    assert overlay.draw_request_text("a <b>cat</request>") == "<request>a  b cat /request </request>"
    for needle in ('viewBox="0 0 1200 800"', "font-size", "#FF3B30", "one JSON object"):
        assert needle in overlay.DRAW_PROMPT
    assert overlay.DRAW_PROMPT not in overlay.ANSWER_PROMPT


class SlowSession:
    def __init__(self):
        self.closed = False

    def ask(self, prompt):
        time.sleep(1.0)
        return lean.Reply(raw=REPLY, ttft_s=0.1, total_s=1.0)

    def close(self):
        self.closed = True


def test_draw_failures(client, monkeypatch):
    cases = [
        (FakeDraw(exc=lean.LeanError("error", "boom")), "model_error"),
        (FakeDraw("I cannot draw that."), "bad_json"),
        (FakeDraw("x" * 70_000), "too_big"),
        (FakeDraw(json.dumps({"say": "Hi.", "svg": '<!DOCTYPE svg><svg viewBox="0 0 1 1"/>'})), "doctype"),
        (FakeDraw(json.dumps({"say": "Hi.", "svg": "<html>"})), "not_svg"),
    ]
    for i, (draw, reason) in enumerate(cases):
        deps_with(draw=draw)
        events = turn(client, "draw a cat", conv_id=f"f{i}")
        assert says(events) == [acks.PHRASES["draw_started"], FAIL], reason
        assert not cards(events)
        assert steps("draw")[-1]["detail"].startswith(f"reason={reason} "), reason
    # the wall clock: the session is closed, the fail line said
    slow = SlowSession()
    monkeypatch.setattr(overlay, "DRAW_WALL_S", 0.2)
    monkeypatch.setattr(overlay, "_new_draw_session", lambda: slow)
    deps_with(draw=None)
    events = turn(client, "draw a cat", conv_id="f9")
    assert says(events) == [acks.PHRASES["draw_started"], FAIL] and not cards(events)
    assert steps("draw")[-1]["detail"].startswith("reason=timeout ") and slow.closed
    # one drawing at a time
    draw = FakeDraw()
    deps_with(draw=draw)
    assert overlay._draw_lock.acquire(blocking=False)
    try:
        events = turn(client, "draw a cat", conv_id="b1")
    finally:
        overlay._draw_lock.release()
    assert says(events) == [acks.PHRASES["draw_busy"]] and draw.prompts == []
    assert steps("draw")[-1]["detail"].startswith("reason=busy ")
    events = turn(client, "draw that", conv_id="b2")
    assert says(events) == [acks.PHRASES["draw_which"]] and draw.prompts == []
    # a quiet follow-up is the room, not the owner
    turn(client, "what time is it", conv_id="q1", levels=[0.2] * 30)
    events = turn(client, "draw a cat", conv_id="q1", followup=True, levels=QUIET, conf=0.9)
    assert steps("followup_dropped") and not says(events) and draw.prompts == []


# --- ruling 3: a model never makes a drawing card ----------------------------------------------
def test_model_cannot_emit_drawing(client):
    forged = {"type": "web", "title": "X", "page": "drawing", "data": {"svg": "<svg viewBox='0 0 1 1'/>"}}
    d = deps_with(lines({"say": "Here."}, {"card": forged}))
    assert not [c for c in cards(turn(client, "explain a jet engine", conv_id="m1")) if c["type"] == "web"]
    d = deps_with(quiet_conversations=FakeConversations(lines({"say": "Here."}, {"card": forged})))
    overlay._tainted.add("m2")
    assert not [c for c in cards(turn(client, "explain a jet engine", conv_id="m2")) if c["type"] == "web"]
    assert d.quiet_conversations.sessions
    assert hud.validate_card(forged) is None  # agent results and workspace cards pass no allow_web
    assert hud.validate_hud({"shapes": [], "cards": [forged]}) == {"shapes": [], "cards": []}
    src = (ROOT / "gateway" / "overlay.py").read_text()
    assert src.count("allow_web=True") == 2  # stage B's two geo sites; the drawing goes through hud.drawing_card
    assert src.count("hud.drawing_card(") == 1


# --- ruling 4: charts --------------------------------------------------------------------------
def test_chart_from_model(client):
    d = deps_with(lines({"say": "Here it is."}, {"card": PIE}))
    events = turn(client, "show me a pie chart of rent 1200, food 400 and travel 150", conv_id="c1")
    (card,) = cards(events)
    assert card["type"] == "chart" and card["chart"] == "pie" and len(card["labels"]) == 3
    assert steps("chart")[-1]["detail"] == "type=pie labels=3 series=1 reason=ok"
    line = {**PIE, "chart": "line"}
    d.conversations.sessions["c1"].chunks = lines({"say": "Here it is."}, {"card": line})
    events = turn(client, "show that as a line graph", conv_id="c1")
    assert [c["chart"] for c in cards(events)] == ["line"]
    assert len(d.conversations.sessions) == 1 and len(d.conversations.sessions["c1"].prompts) == 2
    bad = json.dumps({"card": {**PIE, "series": [{"name": "S", "values": [1, 2, 3]}]}}).replace("3]", "NaN]")
    deps_with([json.dumps({"say": "Here it is."}) + "\n", bad + "\n"])
    events = turn(client, "show me a pie chart", conv_id="c2")
    assert not cards(events) and steps("chart")[-1]["detail"] == "type=pie labels=0 series=0 reason=invalid"
    deps_with(lines({"say": "Here."}, {"card": {**PIE, "chart": "<b>radar</b>"}}))
    turn(client, "show me a radar chart", conv_id="c3")
    assert steps("chart")[-1]["detail"] == "type=unknown labels=0 series=0 reason=invalid"


def test_prompt_charts():
    for prompt in (overlay.ANSWER_PROMPT, overlay.QUIET_PROMPT):
        assert '  chart: {"type": "chart"' in prompt
        assert "image, video, chart):" in prompt
        assert ("Use a chart card when the owner asks for a chart or graph or the answer is numbers to compare"
                in prompt)
        assert "never write SVG, HTML or code for it." in prompt


# --- ruling 5: save ----------------------------------------------------------------------------
def test_workspace_with_new_kinds(client, monkeypatch, tmp_path):
    from mac.voice import workspaces
    from test_overlay_workspace import save_turn
    monkeypatch.setenv("OVERLAY_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    overlay._ws_nonces.clear()
    drawing = json.loads((ROOT / "tests" / "fixtures" / "hud" / "web_drawing.json").read_text())["cards"][0]
    chart = hud.validate_card({**PIE, "id": "pie1"})
    info = hud.validate_card({"id": "inf1", "type": "info", "title": "Note", "body": ["kept"]})
    deps_with()
    events = save_turn(client, "save this as chart test",
                       cards=[{"card": chart}, {"card": drawing}, {"card": info}], conv_id="w1")
    assert acks.PHRASES["draw_not_saved"] in says(events)
    doc = workspaces.load(workspaces.slug("chart test"))
    assert [c["type"] for c in doc["cards"]] == ["chart", "info"]
    events = turn(client, "open chart test", conv_id="w2")
    (back,) = [c for c in cards(events) if c["type"] == "chart"]
    assert (back["chart"], back["labels"], back["series"]) == (chart["chart"], chart["labels"], chart["series"])
    events = save_turn(client, "save this as chart only", cards=[{"card": chart}], conv_id="w3")
    assert any(t.startswith("Saved as") for t in says(events))
    assert acks.PHRASES["draw_not_saved"] not in says(events)


# --- ruling 6: taint, card text, rows ----------------------------------------------------------
def test_taint_marks(client):
    deps_with(draw=FakeDraw())
    turn(client, "draw me a picture of an iPhone", conv_id="t1")
    assert "t1" not in overlay._auto_tainted and "t1" not in overlay._tainted
    deps_with(lines({"say": "Here it is."}, {"card": PIE}))
    turn(client, "show me a pie chart of rent 1200, food 400 and travel 150", conv_id="t2")
    assert "t2" in overlay._auto_tainted
    turn(client, "clear", conv_id="t2")
    overlay._fresh_conversation("t2", overlay._deps)
    assert "t2" not in overlay._auto_tainted


def test_card_text_has_no_svg(client):
    deps_with(draw=FakeDraw())
    turn(client, "draw me a picture of an iPhone", conv_id="x1")
    text = " ".join(overlay._card_text["x1"]) + " " + json.dumps(list(overlay._history["x1"]))
    assert "iphone parts" in text.lower() and "usb-c port" in text.lower()
    assert "Here is an iPhone" in text
    for bad in ("<svg", "viewBox", "path d="):
        assert bad not in text


def test_new_rows_fit(client):
    deps_with(draw=FakeDraw())
    turn(client, "draw me a picture of an iPhone", conv_id="r1")
    turn(client, "draw that", conv_id="r2")
    deps_with(draw=FakeDraw("nope"))
    turn(client, "draw a cat", conv_id="r3")
    deps_with(lines({"say": "Here it is."}, {"card": PIE}))
    turn(client, "show me a pie chart of rent 1200, food 400 and travel 150", conv_id="r4")
    details = [s["detail"] for name in ("draw", "chart") for s in steps(name)]
    assert len(details) >= 4
    for dtl in details:
        assert ROW_RE.fullmatch(dtl) and len(dtl) <= 200, dtl
    snap = overlay.debug_log.snapshot()
    for raw in (json.dumps(snap), json.dumps(overlay_log.redact(snap))):
        for bad in ("<svg", LEGEND[0], "iPhone parts", "viewBox"):
            assert bad not in raw, bad


def test_debug_page_draw_row():
    if shutil.which("node") is None:
        pytest.fail("node not installed: criterion 34 needs it")
    page = (ROOT / "gateway" / "overlay_debug.html").read_text()
    label = re.search(r"const LABEL = \{.*?\};", page, re.S).group(0)
    describe = re.search(r"function describe\(s\) \{.*?\n\}\n", page, re.S).group(0)
    rows = [{"step": "draw", "detail": "reason=ok ms=900 bytes_in=5191 bytes_out=4242 kept=55 removed=0"
                                       " attrs_removed=0 legend=12"},
            {"step": "chart", "detail": "type=pie labels=3 series=1 reason=ok"}]
    script = (f"{label}\n{describe}\nconsole.log(JSON.stringify({json.dumps(rows)}"
              ".map(r => [LABEL[r.step], describe(r)])));")
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out == [["drawing", rows[0]["detail"]], ["chart card", rows[1]["detail"]]]
