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
    assert notify.crossroads(prev, [_s(state="idle", health="idle")]) == []  # a plain turn end is not a crossroads


@pytest.mark.parametrize("extra", [{"spawned_by": "unknown"}, {"parent_session_id": "p"}, {"device": "Mac"}, {"throwaway": True}])
def test_crossroads_skips_non_owner_sessions(extra):
    prev: dict = {}
    notify.crossroads(prev, [_s(**extra)])
    assert notify.crossroads(prev, [_s(state="needs_input", health="waiting", **extra)]) == []


def test_whatsapp_never_sends_under_pytest():
    assert asyncio.run(notify.whatsapp_owner("hi", "s")) is False


def test_router_session_stuck_and_failed_notify_once():
    prev: dict = {}
    notify.crossroads(prev, [_s()])
    assert notify.crossroads(prev, [_s(state="needs_input", health="attention", attention_reason="DONE not accepted")]) == [
        ("attention", "Needs attention: Fix bug", "DONE not accepted", "s1")
    ]
    assert notify.crossroads(prev, [_s(state="needs_input", health="attention")]) == []
    assert notify.crossroads(prev, [_s(state="failed", health="failed")]) == [
        ("failed", "Failed: Fix bug", "Which option?", "s1")
    ]


def test_crossroads_prefers_the_routers_own_question():
    prev: dict = {}
    notify.crossroads(prev, [_s()])
    out = notify.crossroads(prev, [_s(state="needs_input", health="waiting", needs_prompt="Ship it? Yes / No")])
    assert out == [("crossroads", "Needs your decision: Fix bug", "Ship it? Yes / No", "s1")]


def test_router_done_is_task_dones_job_except_glm():
    prev: dict = {}
    notify.crossroads(prev, [_s(), _s("g1", engine="glm")])
    out = notify.crossroads(prev, [_s(state="done", health="done"), _s("g1", state="done", health="done", engine="glm")])
    assert out == [("done", "Done: Fix bug", "Which option?", "g1")]


def test_hook_session_turn_end_is_done_after_a_minute():
    mac = {"spawned_by": "unknown", "device": "Mac"}
    prev: dict = {}
    notify.crossroads(prev, [_s(**mac)], now=0)
    notify.crossroads(prev, [_s(**mac)], now=30)  # still running: start time kept
    assert notify.crossroads(prev, [_s(state="idle", health="idle", **mac)], now=61) == [
        ("done", "Done on Mac: Fix bug", "Which option?", "s1")
    ]
    assert notify.crossroads(prev, [_s(state="idle", health="idle", **mac)], now=200) == []  # once
    assert notify.crossroads(prev, [_s(state="done", health="done", **mac)], now=300) == []  # exit after idle: no 2nd note
    notify.crossroads(prev, [_s(**mac)], now=400)
    assert notify.crossroads(prev, [_s(state="idle", health="idle", **mac)], now=420) == []  # quick turn: no ping


def test_push_app_keeps_notes_for_catch_up(monkeypatch: pytest.MonkeyPatch, gateway_server):
    import httpx

    from conftest import IOS_AUTH_HEADER

    monkeypatch.setattr(notify, "_notes", notify.deque(maxlen=notify.NOTES_KEEP))

    async def run():
        assert await notify.push_app("done", "Done: A", "a", "s1") is False  # app offline: still kept
        await notify.push_app("crossroads", "Needs your decision: B", "b")
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            assert (await http.get("/notify/notes")).status_code == 401
            every = (await http.get("/notify/notes", headers=IOS_AUTH_HEADER)).json()
            newer = (await http.get(f"/notify/notes?after={every[0]['seq']}", headers=IOS_AUTH_HEADER)).json()
        return every, newer

    every, newer = asyncio.run(run())
    assert [n["title"] for n in every] == ["Done: A", "Needs your decision: B"]
    assert every[0]["session_id"] == "s1" and every[0]["seq"] < every[1]["seq"]
    assert [n["title"] for n in newer] == ["Needs your decision: B"]


def test_a_note_counts_as_delivered_only_when_the_app_confirms_it(monkeypatch: pytest.MonkeyPatch):
    from gateway import agents, registry

    monkeypatch.setattr(notify, "_notes", notify.deque(maxlen=notify.NOTES_KEEP))
    monkeypatch.setattr(notify, "NOTE_ACK_S", 0.2)
    sent: list[dict] = []

    def phone(answers: bool):
        async def send_to_role(role, envelope):
            sent.append(envelope)
            if answers:  # the app's reply shape (AppModel.reportResult)
                reply = {"id": "ios-7", "type": "result", "payload": {"id": envelope["id"], "ok": True, "data": {}}}
                asyncio.get_running_loop().call_soon(agents.resolve_reply, reply)
            return True  # a socket is registered either way
        return send_to_role

    async def run():
        monkeypatch.setattr(registry, "send_to_role", phone(answers=True))
        awake = await notify.push_app("crossroads", "Needs your decision: A", "a", "s1", confirm=True)
        monkeypatch.setattr(registry, "send_to_role", phone(answers=False))  # stale socket, app asleep
        asleep = await notify.push_app("crossroads", "Needs your decision: B", "b", "s2", confirm=True)
        unconfirmed = await notify.push_app("done", "Done: C", "c", "s3")  # siri/task_done: send only
        return awake, asleep, unconfirmed

    assert asyncio.run(run()) == (True, False, True)
    assert [e["type"] for e in sent] == ["owner_note"] * 3
    assert agents._pending_replies == {}  # nothing left waiting


def test_resolve_reply_takes_the_ios_result_shape_only_for_a_pending_id():
    from gateway import agents

    assert agents.resolve_reply({"id": "ios-1", "type": "result", "payload": {"id": "note-zz", "ok": True}}) is False
