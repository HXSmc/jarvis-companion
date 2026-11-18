"""jfollow11b: a usage limit is said honestly on every model path and never looks like an outage.

The limit is faked end to end: the env stand-in CLI (tests/fixtures/fake_claude_limit.py) replaces
`claude` in real LeanSession / AgentSession processes, so the real classifier reads a real stream.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import AUTH_HEADER, TOKEN  # noqa: E402
from test_overlay_service import client, make_deps, reset_overlay_state, says, turn  # noqa: E402,F401
from gateway import overlay  # noqa: E402
from mac.voice import intent_pick, lean, youtube  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "fake_claude_limit.py"
LIMIT, CARD = overlay.ANSWER_FAIL["usage_limit"]
BAD = ("not running", "couldn't reach", "fake limit", "429", "resets")
QUESTION = "what is the capital of France"


@pytest.fixture(autouse=True)
def fake_limit(monkeypatch, tmp_path):
    """Every new claude process is the stand-in; its scratch dirs land in tmp_path."""
    monkeypatch.setenv("OVERLAY_CLAUDE_BIN", str(FAKE))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    overlay.debug_log.clear()
    pools: list = []
    yield pools
    for pool in pools:  # end every stand-in process this test started
        for s in [*pool._sessions.values(), pool._spare]:
            if s is not None:
                s.close()


def lean_pool(pools, prompt="", tools=""):
    pool = lean.Conversations(lambda: lean.LeanSession(model="sonnet", system_prompt=prompt, tools=tools))
    pools.append(pool)
    return pool


def limit_deps(pools, **kw):
    d = make_deps(conversations=lean_pool(pools), quiet_conversations=lean_pool(pools), apps={}, **kw)
    overlay.set_deps(d)
    return d


def cards(events):
    return [e["card"] for e in events if e["type"] == "card"]


def text_of(events) -> str:
    """Every event's words (no audio bytes), lower case."""
    return json.dumps([{k: v for k, v in e.items() if k != "pcm_b64"} for e in events]).lower()


def assert_limit_once(events):
    assert says(events).count(LIMIT) == 1, says(events)
    assert [c["title"] for c in cards(events)].count(CARD) == 1
    blob = text_of(events)
    assert not [b for b in BAD if b in blob], blob


def steps(name):
    return [s for t in reversed(overlay.debug_log.snapshot()["turns"]) for s in t["steps"] if s["step"] == name]


# --- Part A: a limit cannot reach the outage gate ------------------------------------------------

def test_health_answers_under_limit(monkeypatch, tmp_path, fake_limit):
    """The service starts with every pool's prewarm and the pick on the stand-in; health still answers."""
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(overlay, "TOKEN_FILE", token_file)
    monkeypatch.setenv("OVERLAY_WAKEWORD", "0")
    from mac.voice import service

    d = limit_deps(fake_limit)
    d.agent_conversations = lean.Conversations(overlay._agent_session, 4)
    fake_limit.append(d.agent_conversations)
    d.intent_pick = intent_pick.Picker().ask
    with TestClient(service.app, client=("127.0.0.1", 50000)) as svc:
        t0 = time.monotonic()
        resp = svc.get("/overlay/health", headers=AUTH_HEADER)
        assert time.monotonic() - t0 < 2.0
        assert resp.status_code == 200 and resp.json()["ok"] is True


def test_events_open_under_limit(client, fake_limit):
    limit_deps(fake_limit)
    with client.stream("GET", "/overlay/events", headers=AUTH_HEADER) as resp:
        assert resp.status_code == 200
        first = next(line for line in resp.iter_lines() if line)
    assert first == "event: hello"


def test_utterance_stream_under_limit(client, fake_limit):
    limit_deps(fake_limit)
    body = {"text": QUESTION, "conv_id": "u1"}
    with client.stream("POST", "/overlay/utterance", json=body, headers=AUTH_HEADER) as resp:
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in resp.iter_lines() if line]
    assert_limit_once(events)


# --- Part B: the limit line on every path --------------------------------------------------------

def test_limit_answer(client, fake_limit):
    limit_deps(fake_limit)
    events = turn(client, QUESTION, conv_id="a1")
    assert_limit_once(events)
    assert events[-1]["type"] == "done"


def test_limit_agent(client, fake_limit):
    """The agent CLI's raw result text ("fake limit") is never spoken or shown: the limit line is."""
    d = limit_deps(fake_limit)
    d.agent_conversations = lean.Conversations(overlay._agent_session, 4)
    fake_limit.append(d.agent_conversations)
    events = turn(client, "list my open pull requests", conv_id="g1")
    assert steps("intent")[-1]["kind"] == "agent"
    assert_limit_once(events)
    assert "I couldn't get an answer for that." not in says(events)


def test_limit_pick(client, fake_limit):
    """The pick's warm-up hits the limit: the row says usage_limit, word rules route on."""
    d = limit_deps(fake_limit)
    picker = intent_pick.Picker()
    deadline = time.monotonic() + 10
    while picker._warming and time.monotonic() < deadline:
        time.sleep(0.05)
    with pytest.raises(lean.LeanError) as exc:
        picker.ask("hello")
    assert exc.value.reason == "usage_limit"
    d.intent_pick = picker.ask
    clock = turn(client, "what time is it", conv_id="p1")  # (a) an instant intent still works
    assert any(s.startswith("It's ") for s in says(clock)) and LIMIT not in says(clock)
    events = turn(client, QUESTION, conv_id="p2")  # (b) the pick is asked, falls back; the answer says it once
    row = steps("intent_pick")[-1]["detail"]
    assert "path=fallback" in row and "reason=usage_limit" in row
    assert_limit_once(events)


def test_limit_sessions(client, monkeypatch, tmp_path, fake_limit):
    """Limit line + card first, then the numbers-only answer."""
    from test_overlay_jfollow10g import BOARD, env
    env(monkeypatch, tmp_path, list(BOARD))
    limit_deps(fake_limit)
    events = turn(client, "how many sessions are running", conv_id="s1")
    assert_limit_once(events)
    spoken = says(events)
    assert spoken.index(LIMIT) < len(spoken) - 1 and spoken[-1].startswith("30 sessions")


def test_limit_draw(client, fake_limit):
    limit_deps(fake_limit).draw_ask = None  # a real lean drawing session on the stand-in
    events = turn(client, "draw a cat", conv_id="d1")
    assert_limit_once(events)
    assert "I couldn't draw that cleanly, sir." not in says(events)
    assert steps("draw")[-1]["detail"].startswith("reason=usage_limit ")


def test_limit_video(client, monkeypatch, fake_limit):
    monkeypatch.setattr(youtube, "transcript", lambda vid: "Rockets burn fuel to climb.")
    limit_deps(fake_limit).video_ask = None  # the real lean video question on the stand-in
    overlay._last_video["v1"] = "aaaaaaaaaaa"
    events = turn(client, "what does the video say about rockets", conv_id="v1")
    assert_limit_once(events)
    assert "reason=usage_limit" in steps("video_ask")[-1]["detail"]


def test_limit_ground(client, fake_limit):
    limit_deps(fake_limit, vlm=overlay._LazyVLM())  # the real grounding session on the stand-in
    events = turn(client, "where is the Send button", conv_id="r1",
                  screen={"scale": 1.0, "ax": [{"role": "AXButton", "label": "Send", "box": [10, 20, 50, 60]}]})
    assert_limit_once(events)
    assert "I can't see it on screen." not in says(events)
    assert "reason=usage_limit" in steps("ground")[-1]["detail"]
