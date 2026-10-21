"""Owner notes: late home-screen answers and router crossroads."""

from __future__ import annotations

import asyncio

import pytest

from gateway import central, notify, siri


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    out: list[tuple] = []

    async def app(kind, title, text, session_id=None):
        out.append(("app", kind, title, text, session_id))
        return True

    async def wa(text, subject):
        out.append(("whatsapp", text))
        return True

    monkeypatch.setattr(notify, "push_app", app)
    monkeypatch.setattr(notify, "whatsapp_owner", wa)
    return out


def test_late_answer_goes_to_app_and_whatsapp(sent):
    async def run():
        task = asyncio.ensure_future(asyncio.sleep(0, result={"action": "answered", "reply": "Use model X."}))
        await siri._deliver_late(task, "best model for <project>?", "voice-a", "m1")

    asyncio.run(run())
    assert sent[0] == ("app", "answer", "Answer: best model for <project>?", "Use model X.", None)
    assert sent[1][0] == "whatsapp" and "Use model X." in sent[1][1]


def test_late_answer_retries_with_same_msg_id(sent, monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def route(thread_id, msg_id, text, source):
        calls.append(msg_id)
        return {"action": "spawned", "session_id": "s-1", "title": "Pick a model"}

    async def failed():
        raise central.RouterError("timeout")

    monkeypatch.setattr(central, "route", route)
    monkeypatch.setattr(siri, "LATE_RETRY_SECONDS", 0)

    async def run():
        await siri._deliver_late(asyncio.ensure_future(failed()), "q", "voice-a", "m1")

    asyncio.run(run())
    assert calls == ["m1"]
    assert sent[0][:3] == ("app", "answer", "Started: Pick a model") and sent[0][4] == "s-1"


def test_late_answer_gives_up_silently(sent, monkeypatch: pytest.MonkeyPatch):
    async def route(*args):
        raise central.RouterError("down")

    async def failed():
        raise central.RouterError("timeout")

    monkeypatch.setattr(central, "route", route)
    monkeypatch.setattr(siri, "LATE_RETRY_SECONDS", 0)
    monkeypatch.setattr(siri, "LATE_ANSWER_MAX_SECONDS", 0.05)

    async def run():
        await siri._deliver_late(asyncio.ensure_future(failed()), "q", "t", "m")

    asyncio.run(run())
    assert sent == []


def _s(sid="s1", state="running", health="working", **kw):
    kw.setdefault("device", "Mini PC")
    return {"session_id": sid, "title": "Fix bug", "state": state, "health": health, "summary": "Which option?", **kw}


def test_crossroads_transitions():
    prev: dict = {}
    assert notify.crossroads(prev, [_s()]) == []  # first sight only records
    assert notify.crossroads(prev, [_s(state="needs_input", health="waiting")]) == [
        ("crossroads", "Needs your decision: Fix bug", "Which option?", "s1")
    ]
    assert notify.crossroads(prev, [_s(state="needs_input", health="waiting")]) == []  # once per transition
    notify.crossroads(prev, [_s()])
    assert notify.crossroads(prev, [_s(state="idle", health="idle")]) == [
        ("turn", "Waiting for you: Fix bug", "Which option?", "s1")
    ]


@pytest.mark.parametrize("extra", [{"spawned_by": "unknown"}, {"parent_session_id": "p"}, {"device": "Mac"}, {"throwaway": True}])
def test_crossroads_skips_non_owner_sessions(extra):
    prev: dict = {}
    notify.crossroads(prev, [_s(**extra)])
    assert notify.crossroads(prev, [_s(state="idle", health="idle", **extra)]) == []


def test_whatsapp_never_sends_under_pytest():
    assert asyncio.run(notify.whatsapp_owner("hi", "s")) is False
