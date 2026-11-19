"""The usage-limit fallback: a limited Claude answer turn (and screen pick) switches to the
local model; agent turns, video ask and draw keep today's limit line.

Claude is a scripted session raising typed failures; the local front is the in-process fake
server (tests/fixtures/fake_local_llm.py), reached only through the real local_llm client.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from test_overlay_service import (  # noqa: E402,F401  (shared stubs + fixtures)
    client, make_deps, reset_overlay_state, says, turn,
)
from test_overlay_answerfail import POOL, Pool, ScriptedSession  # noqa: E402  (shared stubs)
from gateway import overlay  # noqa: E402
from mac.voice import lean, local_llm, youtube  # noqa: E402
from tests.fixtures.fake_local_llm import FakeLocalLLM  # noqa: E402

QUESTION = "what is the capital of France"
SWITCH_LINE, SWITCH_TITLE = overlay.LOCAL_SWITCH
LIMIT_LINE, LIMIT_CARD = overlay.ANSWER_FAIL["usage_limit"]
UNAVAILABLE = overlay.ANSWER_FAIL["local_unavailable"][0]
LOCAL_SLOW = overlay.ANSWER_FAIL["local_timeout"][0]
FAKE = ROOT / "tests" / "fixtures" / "fake_claude_limit.py"


@pytest.fixture(autouse=True)
def local_pools():
    """Track every real Conversations pool this test built; end its sessions after."""
    overlay.debug_log.clear()
    pools: list = []
    yield pools
    for pool in pools:
        for s in [*pool._sessions.values(), pool._spare]:
            if s is not None:
                s.close()


@pytest.fixture
def fake_limit(monkeypatch, tmp_path):
    """The env stand-in CLI for the agent turn: real processes, always limited."""
    monkeypatch.setenv("OVERLAY_CLAUDE_BIN", str(FAKE))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    pools: list = []
    yield pools
    for pool in pools:
        for s in [*pool._sessions.values(), pool._spare]:
            if s is not None:
                s.close()


def set_local(monkeypatch, srv, ttft_s=None) -> None:
    monkeypatch.setenv("OVERLAY_LOCAL_URL", srv.url)
    if ttft_s is not None:
        monkeypatch.setenv("OVERLAY_LOCAL_TTFT_S", str(ttft_s))


def local_pool(pools) -> lean.Conversations:
    pool = lean.Conversations(lambda: local_llm.LocalSession(system_prompt=overlay.answer_prompt()))
    pools.append(pool)
    return pool


def limit_error() -> lean.LeanError:
    return lean.LeanError("usage_limit", "limit")


def make_local_deps(pools, local, claude_session=None, **kw):
    claude = Pool(claude_session if claude_session is not None else ScriptedSession(exc=limit_error()))
    d = make_deps(conversations=claude, quiet_conversations=claude,
                  local_conversations=local, apps={}, **kw)
    overlay.set_deps(d)
    return d, claude


def lines(events) -> list[str]:
    return [s for s in says(events) if s not in POOL]  # the random answer ack never counts


def cards(events):
    return [e["card"] for e in events if e["type"] == "card"]


def completions(srv) -> list[dict]:
    return [r for r in srv.requests if r["path"] == "/v1/chat/completions"]


def done(events) -> dict:
    return [e for e in events if e["type"] == "done"][-1]


def steps(name):
    return [s for t in reversed(overlay.debug_log.snapshot()["turns"]) for s in t["steps"] if s["step"] == name]


# --- the switch --------------------------------------------------------------------------------


def test_limit_answers_with_local(client, monkeypatch, local_pools):
    """A limited Claude ask hands the turn to the local model: switch card + line, then its answer."""
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        d, claude = make_local_deps(local_pools, local_pool(local_pools))
        events = turn(client, QUESTION)
    spoken = lines(events)
    assert SWITCH_LINE in spoken and "Paris." in spoken
    assert spoken.index(SWITCH_LINE) < spoken.index("Paris.")
    assert [c["title"] for c in cards(events)].count(SWITCH_TITLE) == 1
    assert done(events)["error"] is None
    assert claude.dropped == ["c1"]  # the limited CLI was dropped before the local ask
    assert len(completions(srv)) == 1
    assert steps("local")[-1]["detail"].startswith("path=local")


def test_sticky_skips_claude(client, monkeypatch, local_pools):
    """Inside the limit window the next turn never touches the Claude pool (or the intent pick)."""
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        d, claude = make_local_deps(local_pools, local_pool(local_pools))

        class Guard:
            def get(self, conv_id):
                raise AssertionError("the sticky turn must not touch the Claude pool")

            def drop(self, conv_id):
                pass

        turn(client, QUESTION)  # arms the window
        d.conversations = d.quiet_conversations = Guard()
        events = turn(client, QUESTION + " again")
    assert "Paris." in lines(events)
    assert lines(events).count(SWITCH_LINE) == 0  # announced once per window, not per turn
    assert "reason=local_sticky" in steps("intent_pick")[-1]["detail"]
    assert len(completions(srv)) == 2


def test_recheck_returns_to_claude(client, monkeypatch, local_pools):
    """After the recheck window the turn goes back to Claude, and a good answer closes it."""
    now = [1000.0]
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        d, claude = make_local_deps(local_pools, local_pool(local_pools), clock=lambda: now[0])
        first = turn(client, QUESTION)
        now[0] += overlay._local_recheck_s() + 1  # the window expired
        d.conversations = d.quiet_conversations = Pool(ScriptedSession(chunks=['{"say": "Lyon."}\n']))
        second = turn(client, "and the second city")
    assert "Paris." in lines(first) and "Lyon." in lines(second)
    assert lines(second).count(SWITCH_LINE) == 0
    assert overlay._local_until is None  # a fine Claude answer closes the window
    assert len(completions(srv)) == 1  # only turn 1 needed the local model


def test_fallback_off(client, monkeypatch):
    """The env switch off: the plain limit line, and the local front is never contacted."""
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        monkeypatch.setenv("OVERLAY_LOCAL_URL", srv.url)
        monkeypatch.setenv("OVERLAY_LOCAL_FALLBACK", "0")
        assert overlay._local_fallback_on() is False
        claude = Pool(ScriptedSession(exc=limit_error()))
        overlay.set_deps(make_deps(conversations=claude, quiet_conversations=claude, apps={}))
        events = turn(client, QUESTION)
    assert lines(events) == [LIMIT_LINE]
    assert done(events)["error"] == "answer_usage_limit"
    assert completions(srv) == []


def test_not_signed_in_no_local(client, monkeypatch, local_pools):
    """Only a usage limit switches: a sign-in failure keeps today's line, the window never arms."""
    err = lean.LeanError(lean.classify_failure("authentication_failed", None, False,
                                               "Not logged in · Please run /login"), "x")
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools), ScriptedSession(exc=err))
        events = turn(client, QUESTION)
    assert lines(events) == [overlay.ANSWER_FAIL["not_signed_in"][0]]
    assert SWITCH_LINE not in lines(events)
    assert overlay._local_until is None
    assert completions(srv) == []


# --- the local model failing -------------------------------------------------------------------


def test_local_503(client, caplog, monkeypatch, local_pools):
    caplog.set_level(logging.WARNING)
    with FakeLocalLLM(mode="503") as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools))
        events = turn(client, QUESTION)
    assert lines(events) == [UNAVAILABLE]
    assert [c["title"] for c in cards(events)] == [overlay.ANSWER_FAIL["local_unavailable"][1]]
    assert "no backend" not in json.dumps(events)  # never the raw server text
    assert done(events)["error"] == "answer_local_unavailable"
    assert any("class=local_unavailable" in r.getMessage() for r in caplog.records)


def test_local_500(client, caplog, monkeypatch, local_pools):
    caplog.set_level(logging.WARNING)
    with FakeLocalLLM(mode="500") as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools))
        events = turn(client, QUESTION)
    assert lines(events) == [UNAVAILABLE]
    assert done(events)["error"] == "answer_local_unavailable"
    assert any("class=local_unavailable" in r.getMessage() for r in caplog.records)


def test_local_timeout(client, caplog, monkeypatch, local_pools):
    """A hanging local front is cut at its own first-chunk deadline, not the 45 s answer wall."""
    caplog.set_level(logging.WARNING)
    with FakeLocalLLM(mode="hang", hang_s=30.0) as srv:
        set_local(monkeypatch, srv, ttft_s=0.5)
        pool = local_pool(local_pools)
        make_local_deps(local_pools, pool)
        events = turn(client, QUESTION)
    assert lines(events) == [LOCAL_SLOW]
    assert done(events)["error"] == "answer_local_timeout"
    assert any("class=local_timeout" in r.getMessage() for r in caplog.records)
    assert "c1" not in pool  # the hung local session was dropped and closed


# --- the wait ----------------------------------------------------------------------------------


def test_hold_line(client, monkeypatch, local_pools):
    """A slow first local chunk gets the switch line, then one still-working line, then the answer."""
    monkeypatch.setattr(overlay, "LOCAL_HOLD_S", 0.3)
    with FakeLocalLLM(mode="hang", hang_s=1.2, reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools))
        events = turn(client, QUESTION)
    assert lines(events) == [SWITCH_LINE, overlay.LOCAL_HOLD_LINE, "Paris."]
    assert any(c["title"] == SWITCH_TITLE for c in cards(events))


def test_keepalive_on_long_local_wait(client, monkeypatch, local_pools):
    """The local model has no 45 s cap: the stream keeps sending keepalives while it works."""
    monkeypatch.setattr(overlay, "KEEPALIVE_S", 0.2)
    monkeypatch.setattr(overlay, "LOCAL_HOLD_S", 30.0)
    with FakeLocalLLM(mode="hang", hang_s=1.0, reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools))
        events = turn(client, QUESTION)
    assert sum(1 for e in events if e["type"] == "keepalive") >= 2
    assert "Paris." in lines(events)
    assert done(events)["error"] is None


def test_mini_pc_slow_line(client, monkeypatch, local_pools):
    """A loopback upstream means the small machine runs the model: the slow line, once per window."""
    with FakeLocalLLM(reply='{"say": "Paris."}', upstream="127.0.0.1:8411") as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools))
        first = turn(client, QUESTION)
        second = turn(client, QUESTION + " again")  # same window, same slow machine: said once only
        overlay._local_until, overlay._local_slow_said = None, False  # a fresh window on the fast PC
        srv.upstream = "fast-pc:8411"
        third = turn(client, QUESTION + " once more", conv_id="c3")
    assert lines(first).count(overlay.LOCAL_SLOW_LINE) == 1
    assert lines(first).index(overlay.LOCAL_SLOW_LINE) > lines(first).index("Paris.")  # known once it answered
    assert overlay.LOCAL_SLOW_LINE not in lines(second) and "Paris." in lines(second)
    assert overlay.LOCAL_SLOW_LINE not in lines(third) and "Paris." in lines(third)


# --- what never goes local ---------------------------------------------------------------------


def test_agent_turn_no_local(client, fake_limit, monkeypatch):
    """An agent turn under the limit keeps the limit line; the local front is never contacted."""
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        local = local_pool(fake_limit)
        claude = lean.Conversations(lambda: lean.LeanSession(model="sonnet", system_prompt=""))
        fake_limit.append(claude)
        d = make_deps(conversations=claude, quiet_conversations=claude,
                      local_conversations=local, apps={})
        overlay.set_deps(d)
        d.agent_conversations = lean.Conversations(overlay._agent_session, 4)
        fake_limit.append(d.agent_conversations)
        events = turn(client, "list my open pull requests", conv_id="g1")
    assert steps("intent")[-1]["kind"] == "agent"
    assert LIMIT_LINE in lines(events) and SWITCH_LINE not in lines(events)
    assert completions(srv) == []


def test_video_and_draw_no_local(client, monkeypatch, local_pools):
    """Video ask and draw under the limit keep the limit line; only answer turns switch."""

    def boom(*a, **k):
        raise limit_error()

    monkeypatch.setattr(youtube, "transcript", lambda vid: "Rockets burn fuel to climb.")
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        d, _ = make_local_deps(local_pools, local_pool(local_pools),
                               ScriptedSession(chunks=['{"say": "unused"}']))
        d.video_ask = boom
        d.draw_ask = boom
        overlay._last_video["v1"] = "a" * 11
        video = turn(client, "what does the video say about rockets", conv_id="v1")
        draw = turn(client, "draw a cat", conv_id="d1")
    for events in (video, draw):
        assert LIMIT_LINE in lines(events) and SWITCH_LINE not in lines(events)
    assert completions(srv) == []  # neither turn ever reached the local front


# --- the data the local model sees --------------------------------------------------------------


def test_screenshot_text_only(client, monkeypatch, local_pools):
    """A screen-bearing question reaches the local model as scrubbed text; the JPEG never leaves."""
    with FakeLocalLLM(reply='{"say": "It shows an invoice."}') as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools))
        overlay._local_until = time.monotonic() + 900  # deps default clock is time.monotonic
        events = turn(client, "what does this screen say", conv_id="s1",
                      screen={"scale": 1.0,
                              "image_jpeg_b64": base64.b64encode(b"\xff\xd8not-really-jpeg").decode(),
                              "ocr": [{"text": "Invoice total 42", "box": [1, 2, 3, 4]}]})
    assert "It shows an invoice." in lines(events)
    assert "s1" in overlay._tainted  # the screen-bearing conversation stays on the safe path
    req = completions(srv)[-1]
    blob = json.dumps(req["body"])
    assert "image_url" not in blob and "base64" not in blob and "not-really-jpeg" not in blob
    assert all(isinstance(m["content"], str) for m in req["body"]["messages"])
    assert "Invoice total 42" in blob  # the screen text itself did go in, as plain text


def test_no_anthropic_contact(client, fake_limit, monkeypatch, local_pools):
    """While on local no claude process starts, and the local ask carries no Claude credential."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-xyz")
    with FakeLocalLLM(reply='{"say": "Paris."}') as srv:
        set_local(monkeypatch, srv)
        claude = lean.Conversations(lambda: lean.LeanSession(model="sonnet", system_prompt=""))
        fake_limit.append(claude)  # real CLI processes on the always-limited stand-in
        overlay.set_deps(make_deps(conversations=claude, quiet_conversations=claude,
                                   local_conversations=local_pool(local_pools), apps={}))
        events = turn(client, QUESTION)
        popen = []
        monkeypatch.setattr(lean.subprocess, "Popen", lambda *a, **k: popen.append(a) or 1 / 0)
        sticky = turn(client, QUESTION + " again")  # inside the window: no claude process may start
    assert "Paris." in lines(events) and "Paris." in lines(sticky)
    assert popen == []
    assert len(completions(srv)) == 2
    for req in srv.requests:
        assert not any(h in req["headers"] for h in ("authorization", "x-api-key", "cookie"))
        blob = json.dumps(req["headers"]) + json.dumps(req.get("body") or "")
        assert "sk-test-123" not in blob and "tok-xyz" not in blob and "ANTHROPIC" not in blob


def test_tainted_media_refused_on_local(client, monkeypatch, local_pools):
    """A tainted conversation on the local model: a screen-steered media directive is refused."""
    with FakeLocalLLM(reply='{"say": "Sure."}\n{"image_search": {"query": "cat", "count": 1}}\n') as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools))
        overlay._tainted.add("c1")
        overlay._local_until = time.monotonic() + 900  # deps default clock is time.monotonic
        events = turn(client, "tell me about cats", conv_id="c1")
    assert "Sure." in lines(events)
    assert overlay.QUIET_MEDIA_LINE in says(events)  # the honest refusal line
    assert cards(events) == []  # no picture card for a screen-tainted ask
    assert steps("media")[-1]["detail"] == "refused source=model reason=screen_taint"


def test_ground_pick_local(client, monkeypatch, local_pools):
    """A limited VLM screen pick is re-answered by the local model: shape, Here., switch card."""
    calls = []

    def vlm_boom(prompt, jpeg):
        calls.append(prompt)
        raise limit_error()

    with FakeLocalLLM(reply='{"id": 1}', upstream="fast-pc:8411") as srv:
        set_local(monkeypatch, srv)
        make_local_deps(local_pools, local_pool(local_pools),
                        ScriptedSession(chunks=['{"say": "unused"}']), vlm=vlm_boom)
        events = turn(client, "where is the Send button", conv_id="r1",
                      screen={"scale": 1.0, "ax": [{"role": "AXButton", "label": "Send",
                                                    "box": [10, 20, 50, 60]}],
                              "image_jpeg_b64": base64.b64encode(b"\xff\xd8not-really-jpeg").decode()})
    blob = json.dumps(completions(srv)[-1]["body"])
    assert "Send" in blob  # the candidates went as text
    assert "image_url" not in blob and "base64" not in blob and "not-really-jpeg" not in blob
    assert len(calls) == 1  # the VLM was asked once, then the local model took over
    assert any(e["type"] == "shape" for e in events)
    assert "Here." in says(events)
    assert SWITCH_LINE in lines(events) and any(c["title"] == SWITCH_TITLE for c in cards(events))
    assert "via=local" in steps("ground")[-1]["detail"]
    assert len(completions(srv)) == 1
