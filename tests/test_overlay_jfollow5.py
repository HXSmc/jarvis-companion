"""jfollow5: a session call that was sent but got no answer is "unsure", never "failed";
a slow start is confirmed through the lookup; the owner's task text never leaks."""

from __future__ import annotations

import json
import sys
import time
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
# jfollow4's fixtures and helpers; importing `fresh_session_state` makes it autouse here too.
from test_overlay_jfollow4 import (ROWS, _cards, _deps, _session_env, _steps,  # noqa: F401,E402
                                   fresh_session_state)
from test_overlay_service import FakeConversations, says, turn  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_voice_started():
    overlay._voice_started.clear()
    yield
    overlay._voice_started.clear()


def _slow_env(monkeypatch, tmp_path, get=None, post=None):
    """A fake Companion where GET and POST each answer through a callable; `seen` records."""
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url),
                     request.headers.get("authorization"), request.read().decode()))
        if request.method == "GET":
            return httpx.Response(200, json=[]) if get is None else get()
        return httpx.Response(200, json={"ok": True}) if post is None else post(request)

    monkeypatch.setattr(overlay, "_sessions_transport", httpx.MockTransport(handler))
    monkeypatch.setenv("OVERLAY_COMPANION_URL", "http://127.0.0.1:8788")
    token = tmp_path / "companion-token"
    token.write_text("not-a-real-token")
    token.chmod(0o600)
    monkeypatch.setenv("OVERLAY_COMPANION_TOKEN_FILE", str(token))
    return seen


def test_session_start_slow_router(client, monkeypatch, tmp_path):
    """The start POST outlives the feedback window and times out, but the router did start
    the session: the waiting line speaks first, the lookup finds it, the start is confirmed."""
    clock = Clock()
    _deps(clock)
    box = {}
    rows: list = []

    def post(request):
        box["prompt"] = json.loads(request.read())["text"]
        rows[:] = [{"session_id": "new-9", "state": "working", "device": "Mini PC",
                    "title": "Voice test", "task": box["prompt"],
                    "last_activity_at": "2026-11-13T10:00:00Z"}]
        time.sleep(overlay.FEEDBACK_S + 0.3)
        raise httpx.ReadTimeout("slow", request=request)

    seen = _slow_env(monkeypatch, tmp_path, get=lambda: httpx.Response(200, json=rows), post=post)
    monkeypatch.setattr(overlay, "FEEDBACK_S", 0.2)
    turn(client, "start a session to fix the failing tests", conv_id="sl1")
    events = turn(client, "yes", conv_id="sl1")
    assert says(events) == [acks.PHRASES["session_waiting"],
                            "Started Voice test on the mini PC, sir."]
    assert [m for m, _u, _a, _b in seen] == ["POST", "GET"]  # one POST, one lookup, no retry
    assert _steps("session_start")[-1]["detail"] == "confirmed id=new-9 via=lookup"
    assert overlay._session_ref["sl1"] == ["new-9"]
    assert "new-9" in overlay._voice_started


def test_session_start_slow_not_found(client, monkeypatch, tmp_path):
    """Sent, no answer, and the lookup finds nothing: unsure, one POST, never "failed"."""
    clock = Clock()
    _deps(clock)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    # (a) no row's task matches the prompt
    seen = _slow_env(monkeypatch, tmp_path, get=lambda: httpx.Response(200, json=ROWS), post=slow)
    turn(client, "start a session to fix the failing tests", conv_id="na1")
    events = turn(client, "yes", conv_id="na1")
    assert says(events) == [acks.PHRASES["session_unsure"]]
    assert acks.PHRASES["session_failed"] not in says(events)
    assert _steps("session_start")[-1]["detail"] == "unsure"
    assert [m for m, _u, _a, _b in seen] == ["POST", "GET"]
    assert _cards(events)[-1]["title"] == "Session start"
    assert _cards(events)[-1]["body"] == ["Mini PC", "Ask me what my sessions are doing."]

    # (b) the lookup itself answers 500
    seen = _slow_env(monkeypatch, tmp_path,
                     get=lambda: httpx.Response(500, json={"error": "x"}), post=slow)
    turn(client, "start a session to alphabetize the sticker binder", conv_id="na2")
    events = turn(client, "yes", conv_id="na2")
    assert says(events) == [acks.PHRASES["session_unsure"]]
    assert _steps("session_start")[-1]["detail"] == "unsure"
    assert [m for m, _u, _a, _b in seen] == ["POST", "GET"]

    # (c) a row matches but its id is already claimed by this process
    box = {}

    def post_with_task(request):
        box["prompt"] = json.loads(request.read())["text"]
        raise httpx.ReadTimeout("slow", request=request)

    seen = _slow_env(monkeypatch, tmp_path, post=post_with_task, get=lambda: httpx.Response(
        200, json=[{"session_id": "new-9", "state": "working", "device": "Mini PC",
                    "title": "Voice test", "task": box.get("prompt", ""),
                    "last_activity_at": "2026-11-13T10:00:00Z"}]))
    overlay._voice_started.append("new-9")
    turn(client, "start a session to fix the failing tests", conv_id="na3")
    events = turn(client, "yes", conv_id="na3")
    assert says(events) == [acks.PHRASES["session_unsure"]]
    assert _steps("session_start")[-1]["detail"] == "unsure"
    assert [m for m, _u, _a, _b in seen] == ["POST", "GET"]


def test_session_start_clear_failures(client, monkeypatch, tmp_path):
    """Nothing sent, or an explicit 500, keeps today's "failed" wording; the start call
    carries the 30 s start timeout."""
    clock = Clock()
    _deps(clock)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    seen = _slow_env(monkeypatch, tmp_path, post=refused)
    turn(client, "start a session to fix the failing tests", conv_id="cf1")
    events = turn(client, "yes", conv_id="cf1")
    assert says(events) == [acks.PHRASES["session_failed"]]
    assert _steps("session_start")[-1]["detail"] == "refused reason=unreachable"
    assert [m for m, _u, _a, _b in seen] == ["POST"]  # nothing sent, so no lookup GET

    seen = _slow_env(monkeypatch, tmp_path,
                     post=lambda request: httpx.Response(500, json={"error": "x"}))
    turn(client, "start a session to fix the failing tests", conv_id="cf2")
    turn(client, "yes", conv_id="cf2")
    assert _steps("session_start")[-1]["detail"] == "refused reason=http_500"

    assert overlay.SESSION_START_TIMEOUT_S == 30
    calls = []
    real = overlay._companion_call

    def recorder(method, path, json=None, timeout=10.0):
        calls.append((method, path, timeout))
        return real(method, path, json, timeout)

    monkeypatch.setattr(overlay, "_companion_call", recorder)
    _slow_env(monkeypatch, tmp_path)
    turn(client, "start a session to fix the failing tests", conv_id="cf3")
    turn(client, "yes", conv_id="cf3")
    assert calls == [("POST", "/chat", 30)]


def test_session_stop_answer_slow(client, monkeypatch, tmp_path):
    """A stop or an answer whose POST was sent but got no answer: unsure, one POST each."""
    clock = Clock()
    _deps(clock)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    seen = _slow_env(monkeypatch, tmp_path, get=lambda: httpx.Response(200, json=ROWS), post=slow)
    turn(client, "stop the build runner session", conv_id="sa1")
    events = turn(client, "yes", conv_id="sa1")
    assert says(events) == [acks.PHRASES["session_unsure"]]
    assert acks.PHRASES["session_failed"] not in says(events)
    assert _steps("session_stop")[-1]["detail"] == "unsure"
    assert [m for m, _u, _a, _b in seen] == ["GET", "POST"]

    turn(client, "tell the build runner session to go ahead", conv_id="sa2")
    events = turn(client, "yes", conv_id="sa2")
    assert says(events) == [acks.PHRASES["session_unsure"]]
    assert acks.PHRASES["session_failed"] not in says(events)
    assert _steps("session_answer")[-1]["detail"] == "unsure"
    assert [m for m, _u, _a, _b in seen].count("POST") == 2  # one POST each, no retry


def test_session_task_text_never_leaks(client, monkeypatch, tmp_path):
    """The prompt the router's row matched on, and any other row's task text, reach no
    event, debug row, history entry or card."""
    clock = Clock()
    _deps(clock)
    box = {}

    def post(request):
        box["prompt"] = json.loads(request.read())["text"]
        raise httpx.ReadTimeout("slow", request=request)

    owner = "water the cactus plants by the sunny window"
    seen = _slow_env(monkeypatch, tmp_path, post=post, get=lambda: httpx.Response(200, json=[
        {"session_id": "pwn-1", "state": "working", "device": "Mini PC",
         "title": "Other work", "task": "PWNED-TASK erase everything",
         "last_activity_at": "2026-11-13T09:00:00Z"},
        {"session_id": "new-5", "state": "working", "device": "Mini PC",
         "title": "Voice test", "task": box.get("prompt", ""),
         "last_activity_at": "2026-11-13T10:00:00Z"},
    ]))
    turn(client, "start a session to " + owner, conv_id="lk1")
    events = turn(client, "yes", conv_id="lk1")
    assert says(events) == ["Started Voice test on the mini PC, sir."]
    assert _cards(events)[-1]["title"] == "Voice test"
    assert box["prompt"].endswith(f'The owner said: "{owner}"')  # the match was the exact prompt
    blob = json.dumps([events, _cards(events), overlay.debug_log.snapshot(),
                       overlay._history], default=str)
    assert "PWNED-TASK" not in blob  # the other row's task text reached nothing
    assert box["prompt"] not in blob  # the prompt copy the lookup compared reached nothing
    _yes_text, yes_say = overlay._history["lk1"][-1]  # the yes turn's own history entry
    assert owner not in yes_say  # the lookup stored no owner words of its own
    # (the ask turn's raw utterance is in _history by pre-existing speech memory, not the lookup)
    assert _steps("session_start")[-1]["detail"] == "confirmed id=new-5 via=lookup"


def test_host_ask_okay_yes_is_not_a_yes(client, monkeypatch, tmp_path):
    """Only the whole word is a yes: "okay yes" is new talk, the ask is dropped unrun."""
    clock = Clock()
    _deps(clock, conversations=FakeConversations(['{"say": "Sure."}\n']))
    seen = _session_env(monkeypatch, tmp_path)
    overlay._host_ask["oy1"] = overlay.HostAsk(
        kind="start", summary="Start a session", deadline=clock() + overlay.CONFIRM_S,
        payload={"words": "fix the failing tests", "ref_row": None})
    events = turn(client, "okay yes", conv_id="oy1")
    assert seen == []  # not one request: the stored start never ran
    assert "oy1" not in overlay._host_ask  # dropped, but never confirmed
    assert _cards(events)[0]["title"] == "Not done"


def test_ack_policy_card_remove():
    """card_remove speaks without an ack first; the three new lines are short, calm, rendered."""
    assert acks.ack_key("card_remove") is None
    assert acks.PHRASES["removed"] == "Removed, sir."
    rendered: list[str] = []

    def fake_synth(text):
        rendered.append(text)
        return b"pcm", 24000

    cache = acks.AckCache(synth=fake_synth)
    cache.prerender()
    for key in ("removed", "session_waiting", "session_unsure"):
        text = acks.PHRASES[key]
        assert len(text.split()) <= 8, key
        assert "?" not in text and "!" not in text, key
        assert text in rendered, key
        assert cache.get(key) == (text, b"pcm", 24000), key
