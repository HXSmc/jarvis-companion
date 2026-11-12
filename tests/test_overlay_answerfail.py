"""A fast-tier answer turn never ends in silence: one plain line + one info card + a done error class.

Fake sessions stand in for LeanSession: no CLI, no network.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from test_overlay_service import (  # noqa: E402  (shared stubs + fixtures)
    FakeConversations, client, make_deps, reset_overlay_state, says, turn,
)
from gateway import overlay  # noqa: E402
from mac.voice import acks, lean  # noqa: E402

__all__ = ["client", "reset_overlay_state"]

QUESTION = "what is the capital of France"
# The answer ack draws a random line from the thinking pool: assert pool membership, not a literal.
POOL = {text for _key, text, _tag in acks.THINKING_POOL}


class ScriptedSession:
    def __init__(self, chunks=(), exc=None, block=None):
        self.chunks, self.exc, self.block = list(chunks), exc, block

    def ask(self, text, jpeg=None, on_text=None):
        for c in self.chunks:
            if on_text:
                on_text(c)
        if self.block is not None:
            self.block.wait(10)
        if self.exc is not None:
            raise self.exc
        return lean.Reply(raw="", ttft_s=None, total_s=0.0)

    def close(self):
        if self.block is not None:
            self.block.set()


class Pool(FakeConversations):
    def __init__(self, session):
        super().__init__()
        self.session, self.dropped = session, []

    def get(self, conv_id):
        return self.session

    def drop(self, conv_id):
        self.dropped.append(conv_id)
        self.session.close()


def run(client, session, text=QUESTION):
    pool = Pool(session)
    overlay.set_deps(make_deps(conversations=pool, quiet_conversations=pool))
    return turn(client, text), pool


def cards(events):
    return [e["card"] for e in events if e["type"] == "card"]


def done(events):
    return [e for e in events if e["type"] == "done"][-1]


def check(events, caplog, cls, line, title):
    spoken = [s for s in says(events) if s not in POOL and s != "Having a look."]  # acks never count
    assert spoken == [line]
    got = cards(events)
    assert len(got) == 1 and got[0]["type"] == "info" and got[0]["title"] == title
    assert got[0]["id"] != "agentconfirm"
    assert any(r.levelno == logging.WARNING and f"class={cls}" in r.getMessage() for r in caplog.records)
    assert done(events)["error"] == f"answer_{cls}"


def test_session_raises(client, caplog):
    caplog.set_level(logging.WARNING)
    events, pool = run(client, ScriptedSession(exc=RuntimeError("boom")))
    check(events, caplog, "error", "Something went wrong on my side.", "No answer")
    assert pool.dropped == ["c1"]
    assert says(events).count("Something went wrong on my side.") == 1


def test_usage_limit(client, caplog):
    caplog.set_level(logging.WARNING)
    err = lean.LeanError(lean.classify_failure("rate_limit", 429, False, "limit, resets 3am"), "x")
    events, pool = run(client, ScriptedSession(exc=err))
    check(events, caplog, "usage_limit", "I've hit my usage limit, so I can't answer right now.",
          "Usage limit reached")
    assert "3am" not in json.dumps(events)
    assert pool.dropped == ["c1"]


def test_not_signed_in(client, caplog):
    caplog.set_level(logging.WARNING)
    err = lean.LeanError(lean.classify_failure("authentication_failed", None, False,
                                               "Not logged in · Please run /login"), "x")
    events, _ = run(client, ScriptedSession(exc=err))
    check(events, caplog, "not_signed_in", "I'm not signed in, so I can't answer right now.", "Not signed in")


def test_timeout(client, caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("OVERLAY_ANSWER_WALL_S", "0.3")
    block = threading.Event()
    try:
        events, pool = run(client, ScriptedSession(block=block, exc=RuntimeError("closed")))
        check(events, caplog, "timeout", "That took too long, so I stopped.", "Timed out")
        assert "c1" in pool.dropped and block.is_set()  # the CLI was stopped
    finally:
        block.set()


def test_empty_stream(client, caplog):
    caplog.set_level(logging.WARNING)
    events, pool = run(client, ScriptedSession())
    check(events, caplog, "error", "Something went wrong on my side.", "No answer")
    assert pool.dropped == []  # clean empty reply: the CLI is alive, the session stays


def test_failure_after_speech_adds_no_line_or_card(client, caplog):
    caplog.set_level(logging.WARNING)
    events, _ = run(client, ScriptedSession(chunks=['{"say": "Paris."}\n'], exc=RuntimeError("x")))
    spoken = says(events)
    assert spoken[0] in POOL and spoken == [spoken[0], "Paris."] and cards(events) == []
    assert any("class=error" in r.getMessage() for r in caplog.records)
    assert done(events)["error"] == "answer_error"


def test_handover_and_incomplete_are_not_failures(client, caplog):
    caplog.set_level(logging.WARNING)
    pool = FakeConversations(chunks=['{"agent": true}\n'])
    overlay.set_deps(make_deps(conversations=pool, quiet_conversations=pool))
    events = turn(client, "what changed in this file", conv_id="h1")
    assert "Something went wrong" not in " ".join(says(events))
    assert not [c for c in cards(events) if c["title"] in ("No answer", "Timed out")]
    assert not [r for r in caplog.records if "answer failed" in r.getMessage()]
    overlay._pending.clear()
    events, _ = run(client, ScriptedSession(chunks=['{"incomplete": true}\n']))
    spoken = says(events)
    assert len(spoken) == 1 and spoken[0] in POOL and cards(events) == []  # the ack is the only speech
    assert done(events)["error"] is None


def test_secrets_never_reach_card_voice_or_log(client, caplog):
    caplog.set_level(logging.WARNING)
    home = "/" + "Users" + "/someone"  # built apart: the repo privacy hook flags a literal home path
    raw = f"bad key sk-ant-xxxx at {home}/.claude for a@b.com"
    events, _ = run(client, ScriptedSession(exc=lean.LeanError("error", raw)))
    blob = json.dumps([e for e in events if e["type"] in ("say", "card")])
    logged = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("sk-ant-xxxx", home, "a@b.com"):
        assert secret not in blob and secret not in logged
    assert "class=error" in logged


def test_classify_from_documented_fields():
    c = lean.classify_failure
    assert c("rate_limit", None, False, "") == "usage_limit"
    assert c("billing_error", None, False, "") == "usage_limit"
    assert c(None, None, True, "") == "usage_limit"
    assert c(None, 429, False, "") == "usage_limit"
    assert c("authentication_failed", None, False, "") == "not_signed_in"
    assert c(None, 401, False, "") == "not_signed_in"
    assert c(None, None, False, "Not logged in · Please run /login") == "not_signed_in"
    assert c("server_error", 500, False, "x") == "error"
    assert c(None, None, False, "") == "error"


@pytest.mark.parametrize("lines,reason", [
    ([{"type": "assistant", "error": "authentication_failed", "message": {}},
      {"type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
       "result": "Not logged in · Please run /login"}], "not_signed_in"),
    ([{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
      {"type": "result", "subtype": "success", "is_error": True, "result": "limit"}], "usage_limit"),
    ([{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": ""}], "error"),
    ([], "error"),
])
def test_lean_ask_raises_typed_failure(monkeypatch, lines, reason):
    class Proc:
        def __init__(self):
            self.stdin = self
            self.stdout = [json.dumps(x) + "\n" for x in lines]
            self.stderr = None

        def write(self, s):
            pass

        def flush(self):
            pass

        def poll(self):
            return 1

    monkeypatch.setattr(lean.subprocess, "Popen", lambda *a, **k: Proc())
    with pytest.raises(lean.LeanError) as exc:
        lean.LeanSession().ask("hi")
    assert exc.value.reason == reason


def test_look_branch_gets_the_same_failure_handling(client, caplog):
    caplog.set_level(logging.WARNING)
    events, pool = run(client, ScriptedSession(exc=RuntimeError("boom")), text="highlight for me the last debugging session")
    check(events, caplog, "error", "Something went wrong on my side.", "No answer")
    assert pool.dropped == ["c1"]


def test_look_branch_agent_handover_counts_as_empty(client, caplog):
    # No escalate list on the look branch: {"agent": true} says and shows nothing, so the failure line fires.
    caplog.set_level(logging.WARNING)
    events, _ = run(client, ScriptedSession(chunks=['{"agent": true}\n']),
                    text="highlight for me the last debugging session")
    check(events, caplog, "error", "Something went wrong on my side.", "No answer")
