"""Local-only chats (router `local_only: true`): no cloud seam, no content in notes.

C11 (plain words skip), C12 (content-free notifications + router error detail),
C13/C14 (siri: no quick.ask/follow-up, phone only, fixed late-answer notes).
Everything runs against the in-process stub router; quick.ask, _run_claude_plain,
whatsapp_owner, push_ntfy and push_app are patched to raise/record.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway import central, notify, quick, siri
from gateway.stubs import router_stub
from tests.test_central import IOS, TOKEN, _call, stub_router  # noqa: F401  (autouse fixture)

LOCAL_ENGINE = "local-qwen"


@pytest.fixture(autouse=True)
def fresh_engines_cache(monkeypatch: pytest.MonkeyPatch):
    """No leakage between tests: the 60 s cache starts unknown and tests seed it."""
    monkeypatch.setattr(central, "_engines_cache",
                        {"at": 0.0, "ids": frozenset(), "local": frozenset()})


@pytest.fixture(autouse=True)
def cloud_seams_raise(monkeypatch: pytest.MonkeyPatch):
    """Every cloud seam records and raises: a local chat reaching one fails the test."""
    from gateway import notify as _notify

    def boom(name):
        def fake(*a, **kw):
            raise AssertionError(f"cloud seam hit: {name}")
        return fake

    monkeypatch.setattr(central, "_run_claude_plain", boom("_run_claude_plain"))
    monkeypatch.setattr(quick, "ask", boom("quick.ask"))
    monkeypatch.setattr(_notify, "whatsapp_owner", boom("whatsapp_owner"))
    monkeypatch.setattr(_notify, "push_ntfy", boom("push_ntfy"))


def _seed_local_engine(monkeypatch: pytest.MonkeyPatch, engine: str = LOCAL_ENGINE) -> None:
    monkeypatch.setattr(central, "_engines_cache",
                        {"at": time.monotonic(), "ids": frozenset({engine}),
                         "local": frozenset({engine})})


def _router_db(monkeypatch: pytest.MonkeyPatch, threads: dict[str, int]) -> None:
    """A throwaway in-memory router DB for siri._thread_is_local: {thread_id: local_only}."""
    import sqlite3

    def connect():
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript("CREATE TABLE sessions (id TEXT, local_only INTEGER);"
                        "CREATE TABLE thread_map (thread_id TEXT, session_id TEXT);")
        for i, (thread, flag) in enumerate(threads.items()):
            c.execute("INSERT INTO sessions VALUES (?, ?)", (f"s{i}", flag))
            c.execute("INSERT INTO thread_map VALUES (?, ?)", (thread, f"s{i}"))
        return c

    monkeypatch.setattr(siri.cards, "_db", connect)


def _local_row(sid: str = "s-loc", **kw) -> dict:
    row = {"session_id": sid, "kind": "claude", "title": "Local chat", "state": "running",
           "cwd": "/tmp", "updated_at": "2026-11-19T10:00:00Z", "summary": "SECRET_SUMMARY_X",
           "local_only": True, "upstream": "mini-pc"}
    row.update(kw)
    return row


# --- C11: plain words never touch a local chat -----------------------------------

def test_plain_skips_local(monkeypatch, stub_router):
    seen: list[str] = []

    def gen(session):
        seen.append(session["session_id"])
        return None

    monkeypatch.setattr(central, "_generate_plain", gen)
    # A stale model-made cache line must not surface either: the router title wins.
    central._save_state({"cleared_before": None, "plain": {
        "s-loc": {"hash": "x", "doing": "SECRET_MODEL_DOING", "recap": ["SECRET_MODEL_RECAP"]},
    }})
    monkeypatch.setattr(router_stub, "SESSIONS", [
        _local_row(summary="SECRET_SUMMARY_X"),
        {"session_id": "s-run", "kind": "claude", "title": "Fix login", "state": "running",
         "updated_at": "2026-11-19T10:00:00Z", "summary": "Patching auth"},
    ])

    from gateway.app import app as gateway_app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app),
                                     base_url="http://gw") as client:
            resp = await client.get("/central/sessions", headers=IOS)
            await asyncio.gather(*list(central._tasks))  # fire-and-forget workers finish first
            return resp

    resp = asyncio.run(run())
    assert resp.status_code == 200, resp.text
    rows = {r["session_id"]: r for r in resp.json()}
    assert seen == ["s-run"]  # only the normal row ever asks the model
    assert rows["s-loc"]["doing"] == "Local chat"
    assert rows["s-loc"]["recap"] == []


# --- C12: notifications are content-free ------------------------------------------

def test_crossroads_local_content_free():
    prev: dict = {}
    assert notify.crossroads(prev, [_local_row()]) == []  # first sight only records
    out = []
    for kw in ({"state": "needs_input", "health": "waiting", "needs_prompt": "SECRET_Q_5521"},
               {"state": "needs_input", "health": "attention", "attention_reason": "SECRET_R_5521",
                "summary": "SECRET_R_5521"},
               {"state": "failed", "health": "failed", "summary": "SECRET_S_5521"}):
        out += notify.crossroads(prev, [_local_row(**kw)])
    needs = "A local-only chat needs you. Open the Jarvis app."
    assert out == [("crossroads", "Local chat", needs, "s-loc"),
                   ("attention", "Local chat", needs, "s-loc"),
                   ("failed", "Local chat", needs, "s-loc")]
    assert "SECRET_" not in str(out)


def test_deliver_late_local_content_free(monkeypatch):
    sent: list[tuple] = []

    async def app(kind, title, text, session_id=None):
        sent.append(("app", kind, title, text, session_id))
        return True

    async def wa(text, subject):
        sent.append(("whatsapp", text, subject))
        return True

    monkeypatch.setattr(notify, "push_app", app)
    monkeypatch.setattr(notify, "whatsapp_owner", wa)

    async def run():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "answered", "session_id": "s-loc", "title": "Local chat",
            "reply": "SECRET_A_5521"}))
        await siri._deliver_late(task, "SECRET_T_5521 hi", "voice-a", "m1", local=True)

    asyncio.run(run())
    assert sent == [("app", "answer", "Local chat",
                     "The local model answered. Open the Jarvis app to read it.", "s-loc"),
                    ("whatsapp", "The local model answered. Open the Jarvis app to read it.",
                     "[jarvis] Local chat")]
    assert "SECRET_" not in str(sent)


# --- C13/C14: siri never uses a cloud seam for a local engine ---------------------

def test_siri_local_no_quick(monkeypatch, stub_router):
    routed: list[dict] = []

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append({"text": text, "engine": engine})
        return {"action": "answered", "session_id": None, "reply": "SECRET_A_5521"}

    monkeypatch.setattr(central, "route", route)
    _seed_local_engine(monkeypatch)
    for text in ("? what is x", "q: x"):
        resp = _call("POST", "/siri/hey", headers=IOS, json={"text": text, "engine": LOCAL_ENGINE})
        assert resp.status_code == 200, resp.text
        assert resp.json()["reply"] == "SECRET_A_5521"
    assert routed == [{"text": "? what is x", "engine": LOCAL_ENGINE},
                      {"text": "q: x", "engine": LOCAL_ENGINE}]  # prefix stays in the text

    # The shared-token Shortcut may not use the local model at all.
    shared = {"Authorization": f"Bearer {TOKEN}"}
    resp = _call("POST", "/siri/hey", headers=shared, json={"text": "? what is x",
                                                            "engine": LOCAL_ENGINE})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "The local model works only from the Jarvis app."


def test_siri_non_local_still_uses_quick(monkeypatch, stub_router):
    asked: list[str] = []

    async def fake_ask(q: str, brain: str = "auto", codes: bool = False) -> quick.QuickAnswer:
        asked.append(q)
        return quick.QuickAnswer("quick answer", "web", 1, None)

    monkeypatch.setattr(quick, "ask", fake_ask)
    monkeypatch.setattr(siri, "roles", lambda: [])
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "? what is x"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reply": "quick answer"}
    assert asked == ["what is x"]


# --- router error detail passthrough (local errors only) ---------------------------

def test_local_error_detail_passthrough(monkeypatch, stub_router):
    def fake_router(status: int, detail: str | None) -> FastAPI:
        app = FastAPI()

        @app.post("/route")
        async def route(request: Request):
            return JSONResponse({"detail": detail} if detail else {}, status_code=status)

        return app

    def client(status: int, detail: str | None):
        monkeypatch.setattr(central, "_client",
                            lambda timeout: httpx.AsyncClient(
                                transport=httpx.ASGITransport(app=fake_router(status, detail)),
                                timeout=timeout))

    client(503, "Local model unavailable: both machines are off. Nothing was sent to any other model.")
    resp = _call("POST", "/chat", headers=IOS,
                 json={"thread_id": "app-1", "msg_id": "m-1", "text": "hi",
                       "engine": LOCAL_ENGINE})
    assert resp.status_code == 503
    assert resp.json()["detail"] == ("Local model unavailable: both machines are off. "
                                     "Nothing was sent to any other model.")

    client(500, "any internal detail")
    resp = _call("POST", "/chat", headers=IOS,
                 json={"thread_id": "app-1", "msg_id": "m-1", "text": "hi"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "router error 500"


# --- key passthrough ----------------------------------------------------------------

def test_engines_local_only_passthrough(monkeypatch, stub_router):
    data = {"engines": [
        {"id": "glm-4", "label": "GLM", "kind": "cloud", "available": True, "default": True,
         "reason": None, "local_only": False},
        {"id": LOCAL_ENGINE, "label": "Qwen 27B · Local only", "kind": "local", "available": True,
         "default": False, "reason": None, "local_only": True},
    ]}

    async def fake_json(method: str, path: str, body=None):
        return data

    monkeypatch.setattr(central, "_router_json", fake_json)
    resp = _call("GET", "/central/engines", headers=IOS)
    assert resp.status_code == 200, resp.text
    engines = {e["id"]: e for e in resp.json()["engines"]}
    assert engines[LOCAL_ENGINE]["local_only"] is True
    assert engines["glm-4"]["local_only"] is False
    assert central._engines_cache["local"] == frozenset({LOCAL_ENGINE})


def test_route_and_session_upstream_passthrough(monkeypatch, stub_router):
    async def fake_json(method: str, path: str, body=None):
        if path == "/route":
            return {"action": "answered", "session_id": None, "reply": "42",
                    "upstream": "gaming-pc"}
        if path == "/sessions":
            return [_local_row()]
        raise AssertionError(path)

    monkeypatch.setattr(central, "_router_json", fake_json)
    resp = _call("POST", "/chat", headers=IOS,
                 json={"thread_id": "app-1", "msg_id": "m-1", "text": "hi"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["upstream"] == "gaming-pc"

    monkeypatch.setattr(router_stub, "SESSIONS", [_local_row()])
    rows = _call("GET", "/central/sessions", headers=IOS).json()
    assert rows[0]["local_only"] is True
    assert rows[0]["upstream"] == "mini-pc"


# --- the zero-seam proof -------------------------------------------------------------

def test_proof_gateway_zero_seams(monkeypatch, stub_router):
    hits: list[str] = []

    def watch(name):
        def fake(*a, **kw):
            hits.append(name)
            raise AssertionError(f"cloud seam hit: {name}")
        return fake

    def record(name):
        def fake(*a, **kw):
            hits.append(f"{name}({a!r})") if any("SECRET_" in str(x) for x in a) else None
            return None
        return fake

    monkeypatch.setattr(central, "_run_claude_plain", watch("_run_claude_plain"))
    monkeypatch.setattr(quick, "ask", watch("quick.ask"))

    def note_watch(name):
        async def fake(*a, **kw):
            if any("SECRET_" in str(x) for x in a):
                hits.append(f"{name}({a!r})")
            return True
        return fake

    monkeypatch.setattr(notify, "whatsapp_owner", note_watch("whatsapp_owner"))
    monkeypatch.setattr(notify, "push_ntfy", note_watch("push_ntfy"))

    async def app_push(kind, title, text, session_id=None, confirm=False):
        if "SECRET_" in f"{title}{text}":
            hits.append(f"push_app({title!r},{text!r})")
        return True

    monkeypatch.setattr(notify, "push_app", app_push)

    # 1. /central/sessions poll with a local row (+ the model path patched to raise).
    monkeypatch.setattr(central, "_generate_plain", watch("_generate_plain"))
    monkeypatch.setattr(router_stub, "SESSIONS", [_local_row(summary="SECRET_SUMMARY_X")])
    resp = _call("GET", "/central/sessions", headers=IOS)
    assert resp.status_code == 200, resp.text

    # 2. crossroads failed transition on the local row: fixed strings only.
    prev: dict = {}
    notify.crossroads(prev, [_local_row()])
    for note in notify.crossroads(prev, [_local_row(state="failed", health="failed",
                                                    summary="SECRET_S_5521")]):
        if "SECRET_" in str(note):
            hits.append(f"crossroads({note!r})")

    # 3. siri_hey with a local engine and a quick prefix: straight to the router.
    routed: list[dict] = []

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append({"text": text, "engine": engine})
        return {"action": "answered", "session_id": "s-loc", "reply": "SECRET_A_5521"}

    monkeypatch.setattr(central, "route", route)
    _seed_local_engine(monkeypatch)
    resp = _call("POST", "/siri/hey", headers=IOS,
                 json={"text": "? SECRET_T_5521", "engine": LOCAL_ENGINE})
    assert resp.status_code == 200, resp.text
    assert routed and routed[0]["text"] == "? SECRET_T_5521"

    # 4. late delivery for the local engine: content-free notes only.
    async def late():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "answered", "session_id": "s-loc", "title": "Local chat",
            "reply": "SECRET_A_5521"}))
        await siri._deliver_late(task, "SECRET_T_5521", "voice-a", "m1", local=True)

    asyncio.run(late())

    # 5. (round 1, S2) a `?` follow-up with NO engine on a local thread: no quick.ask,
    # and a late answer marked local by the router reply alone stays content-free.
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {"thr-proof": 1})
    resp = _call("POST", "/siri/hey", headers=IOS,
                 json={"text": "? SECRET_T_5521", "thread_id": "thr-proof"})
    assert resp.status_code == 200, resp.text

    async def late_unflagged():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "queued", "session_id": "s-loc", "kind": LOCAL_ENGINE,
            "title": "Local chat", "reply": "SECRET_A_5521", "upstream": "mini-pc"}))
        await siri._deliver_late(task, "SECRET_T_5521", "thr-proof", "m2")

    asyncio.run(late_unflagged())

    print(f"GATEWAY_CLOUD_HITS={len(hits)}")
    assert hits == []


# --- round 1, S2: a follow-up on a local thread is local, whatever engine it names --

def test_siri_local_thread_followup_no_quick(monkeypatch, stub_router):
    """The picker resets to Hub: a `?` follow-up on a local thread names no engine."""
    routed: list[dict] = []

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append({"thread": thread_id, "text": text, "engine": engine})
        return {"action": "queued", "session_id": "s-loc", "kind": LOCAL_ENGINE,
                "title": "Local chat", "upstream": "mini-pc"}

    monkeypatch.setattr(central, "route", route)
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {"thr-local-1": 1})  # router DB: this thread maps to a local_only row
    for text in ("? SECRET_T_5521", "q: SECRET_T_5521"):
        resp = _call("POST", "/siri/hey", headers=IOS,
                     json={"text": text, "thread_id": "thr-local-1"})
        assert resp.status_code == 200, resp.text
    assert [r["text"] for r in routed] == ["? SECRET_T_5521", "q: SECRET_T_5521"]
    assert all(r["thread"] == "thr-local-1" for r in routed)
    # the first reply marks the thread with its local engine (R4-1); the next one carries it
    assert [r["engine"] for r in routed] == [None, LOCAL_ENGINE]

    # the shared-token Shortcut cannot reach a local thread either
    shared = {"Authorization": f"Bearer {TOKEN}"}
    resp = _call("POST", "/siri/hey", headers=shared,
                 json={"text": "? SECRET_T_5521", "thread_id": "thr-local-1"})
    assert resp.status_code == 403


def test_siri_local_thread_remembered_from_reply(monkeypatch, stub_router):
    """A router reply marked local (upstream) makes the thread local for this process."""
    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        return {"action": "spawned", "session_id": "s-loc", "kind": LOCAL_ENGINE,
                "title": "Local chat", "upstream": "unknown"}

    monkeypatch.setattr(central, "route", route)
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {})
    _seed_local_engine(monkeypatch)
    resp = _call("POST", "/siri/hey", headers=IOS,
                 json={"text": "hello", "thread_id": "thr-local-2", "engine": LOCAL_ENGINE})
    assert resp.status_code == 200, resp.text
    assert "thr-local-2" in siri._LOCAL_THREADS
    assert siri._thread_is_local("thr-local-2")


def test_deliver_late_reply_marked_local_is_content_free(monkeypatch):
    """No engine and no local flag from the request: the router's reply alone decides."""
    sent: list[tuple] = []

    async def app(kind, title, text, session_id=None):
        sent.append(("app", title, text))
        return True

    async def wa(text, subject):
        sent.append(("whatsapp", text, subject))
        return True

    monkeypatch.setattr(notify, "push_app", app)
    monkeypatch.setattr(notify, "whatsapp_owner", wa)

    async def run():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "queued", "session_id": "s-loc", "kind": LOCAL_ENGINE,
            "title": "Local chat", "reply": "SECRET_A_5521", "upstream": "gaming-pc"}))
        await siri._deliver_late(task, "SECRET_T_5521 follow-up", "thr-local-3", "m1")

    asyncio.run(run())
    assert sent and "SECRET_" not in str(sent)
    assert sent[0] == ("app", "Local chat", "The local model answered. Open the Jarvis app to read it.")


# --- round 2: G2 (mark the thread before routing), G3 (unreadable DB fails closed) ---

def test_siri_followup_while_first_local_in_flight(monkeypatch, stub_router):
    """G2 box race: msg 1 (local) is still inside the router call when a `?` follow-up
    with no engine arrives on the same thread. The DB has no thread_map row yet."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {})  # nothing mapped yet
    _seed_local_engine(monkeypatch)
    routed: list[dict] = []

    async def scenario():
        gate = asyncio.Event()

        async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
            routed.append({"text": text, "engine": engine})
            if engine == LOCAL_ENGINE:
                await gate.wait()  # msg 1 held in flight
            return {"action": "queued", "session_id": "s-loc", "kind": LOCAL_ENGINE,
                    "upstream": "unknown"}

        monkeypatch.setattr(central, "route", route)
        from gateway.app import app as gw_app
        transport = httpx.ASGITransport(app=gw_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t1 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "first", "thread_id": "thr-race", "engine": LOCAL_ENGINE}))
            for _ in range(50):
                if routed:
                    break
                await asyncio.sleep(0.01)
            t2 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "? SECRET_GW_RACE", "thread_id": "thr-race"}))
            await asyncio.sleep(0.05)
            gate.set()
            r1, r2 = await asyncio.gather(t1, t2)
        return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert [r["text"] for r in routed] == ["first", "? SECRET_GW_RACE"]  # quick.ask never ran


def test_siri_mac_no_db_after_timeout_stays_local(monkeypatch, stub_router):
    """G2 Mac case: no router DB; msg 1 times out at the gateway and is delivered late.
    The thread must already be local, so a `?` follow-up never reaches quick.ask."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    monkeypatch.setattr(siri.cards, "_db", lambda: None)
    sent: list[tuple] = []

    async def app_push(kind, title, text, session_id=None):
        sent.append((title, text))
        return True

    async def wa(text, subject):
        sent.append((subject, text))
        return True

    monkeypatch.setattr(notify, "push_app", app_push)
    monkeypatch.setattr(notify, "whatsapp_owner", wa)

    async def late():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "spawned", "session_id": "s-loc", "kind": LOCAL_ENGINE,
            "title": "Local chat", "upstream": "unknown", "reply": "SECRET_A"}))
        await siri._deliver_late(task, "SECRET_T first", "thr-mac", "m1", local=True)

    asyncio.run(late())
    assert "thr-mac" in siri._LOCAL_THREADS and "SECRET_" not in str(sent)
    monkeypatch.setattr(siri.cards, "_db", lambda: None)
    routed: list[str] = []

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append(text)
        return {"action": "queued", "session_id": "s-loc", "kind": LOCAL_ENGINE}

    monkeypatch.setattr(central, "route", route)
    resp = _call("POST", "/siri/hey", headers=IOS,
                 json={"text": "? SECRET_GW_MAC", "thread_id": "thr-mac"})
    assert resp.status_code == 200, resp.text
    assert routed == ["? SECRET_GW_MAC"]


def test_siri_unreadable_db_fails_closed(monkeypatch, stub_router):
    """G3: a router DB that cannot answer (absent, error, older schema) counts as local
    for a request with a client thread: no quick.ask; the router decides."""
    import sqlite3

    class Broken:
        def execute(self, *a):
            raise sqlite3.OperationalError("no such column: s.local_only")

        def close(self):
            pass

    routed: list[str] = []

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append(text)
        return {"action": "answered", "session_id": None, "reply": "ok"}

    monkeypatch.setattr(central, "route", route)
    for db in (lambda: None, lambda: Broken()):
        monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
        monkeypatch.setattr(siri.cards, "_db", db)
        resp = _call("POST", "/siri/hey", headers=IOS,
                     json={"text": "? SECRET_GW_DB", "thread_id": "thr-unknown"})
        assert resp.status_code == 200, resp.text
    assert routed == ["? SECRET_GW_DB", "? SECRET_GW_DB"]
    # a readable DB that says "not local" keeps today's quick path
    asked: list[str] = []

    async def fake_ask(q, brain="auto", codes=False):
        asked.append(q)
        return quick.QuickAnswer("quick", "web", 1, None)

    monkeypatch.setattr(quick, "ask", fake_ask)
    monkeypatch.setattr(siri, "roles", lambda: [])
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {"thr-hub": 0})
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "? what is x", "thread_id": "thr-hub"})
    assert resp.status_code == 200 and asked == ["what is x"]


# --- round 3: R3-1 (cold engines cache window), "cannot tell" notice, set cap ---

def _held_engines(monkeypatch, held: asyncio.Event, gate: asyncio.Event) -> None:
    """GET /engines (cold cache) parks until `gate` is set; `held` says message 1 is parked there."""
    async def router_json(method, path, *a, **kw):
        assert (method, path) == ("GET", "/engines"), (method, path)
        held.set()
        await gate.wait()
        return {"engines": [{"id": LOCAL_ENGINE, "local_only": True}]}

    monkeypatch.setattr(central, "_router_json", router_json)


def _window_run(monkeypatch, follow_up: str):
    """Message 1 names the local engine and is parked in the engines fetch; a follow-up with
    no engine arrives on the same, still unmapped thread. Returns (r1, r2, routed, asked)."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {})  # readable DB, nothing mapped yet: "cannot tell" must not mask the bug
    routed: list[tuple] = []
    asked: list[str] = []

    async def fake_ask(q, brain="auto", codes=False):
        asked.append(q)
        return quick.QuickAnswer("quick", "web", 1, None)

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append((text, engine))
        return {"action": "queued", "session_id": "s-loc", "kind": LOCAL_ENGINE, "upstream": "unknown"}

    monkeypatch.setattr(quick, "ask", fake_ask)
    monkeypatch.setattr(central, "route", route)

    async def scenario():
        held, gate = asyncio.Event(), asyncio.Event()
        _held_engines(monkeypatch, held, gate)
        from gateway.app import app as gw_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw_app), base_url="http://t") as c:
            t1 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "first", "thread_id": "thr-window", "engine": LOCAL_ENGINE}))
            await asyncio.wait_for(held.wait(), 2)
            t2 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": follow_up, "thread_id": "thr-window"}))
            await asyncio.sleep(0.05)  # without the fix, message 2 is done by now
            gate.set()
            return await asyncio.gather(t1, t2)

    r1, r2 = asyncio.run(scenario())
    return r1, r2, routed, asked


def test_siri_cold_cache_window_quick_followup(monkeypatch, stub_router):
    """R3-1 PoC WINDOW: a `?` follow-up while message 1 waits on a cold engines fetch
    must reach the router after message 1, never quick.ask."""
    r1, r2, routed, asked = _window_run(monkeypatch, "? SECRET_R3_WINDOW")
    assert asked == [], f"leak: quick.ask got {asked}"
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert routed == [("first", LOCAL_ENGINE), ("? SECRET_R3_WINDOW", LOCAL_ENGINE)]  # R4-1


def test_siri_cold_cache_window_plain_followup(monkeypatch, stub_router):
    """R3-1 PoC WINDOW_PLAIN: a plain follow-up must not reach the router before message 1
    (an unmapped thread there means classify and a Claude spawn)."""
    r1, r2, routed, asked = _window_run(monkeypatch, "SECRET_R3_PLAIN")
    assert routed[:1] == [("first", LOCAL_ENGINE)], f"leak: router arrival order {routed}"
    assert routed == [("first", LOCAL_ENGINE), ("SECRET_R3_PLAIN", LOCAL_ENGINE)] and asked == []  # R4-1
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)


def test_siri_no_db_hub_reply_not_marked_and_neutral_late_notice(monkeypatch, stub_router):
    """R3 take-now: local only because the DB cannot tell (Mac). The thread is not
    marked, and a late answer gets a neutral app notice, no WhatsApp, no content."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    monkeypatch.setattr(siri.cards, "_db", lambda: None)

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        return {"action": "answered", "session_id": None, "reply": "ok"}

    monkeypatch.setattr(central, "route", route)
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "what is x", "thread_id": "thr-hub5"})
    assert resp.status_code == 200, resp.text
    assert "thr-hub5" not in siri._LOCAL_THREADS
    sent: list[tuple] = []

    async def app_push(kind, title, text, session_id=None):
        sent.append(("app", title, text))
        return True

    async def wa(text, subject):
        sent.append(("wa", subject, text))
        return True

    monkeypatch.setattr(notify, "push_app", app_push)
    monkeypatch.setattr(notify, "whatsapp_owner", wa)

    async def late():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "answered", "session_id": None, "reply": "SECRET_R3_REPLY"}))
        await siri._deliver_late(task, "SECRET_R3_Q", "thr-hub6", "m1", local=True, unsure=True)

    asyncio.run(late())
    assert sent == [("app", "Answer ready", "Your answer is ready. Open the Jarvis app.")]
    assert "thr-hub6" not in siri._LOCAL_THREADS


def test_local_threads_capped(monkeypatch):
    """R3 take-now: the set of local threads keeps the newest LOCAL_THREADS_MAX ids."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    for i in range(siri.LOCAL_THREADS_MAX + 5):
        siri._mark_local(f"t{i}")
    assert len(siri._LOCAL_THREADS) == siri.LOCAL_THREADS_MAX
    assert "t0" not in siri._LOCAL_THREADS and f"t{siri.LOCAL_THREADS_MAX + 4}" in siri._LOCAL_THREADS


# --- round 4: R4-1 (a marked thread keeps its local engine), late local 503 note ---

LOCAL_503 = "Local model unavailable: both machines are off. Nothing was sent to any other model."


def _local_503() -> central.RouterError:
    err = central.RouterError("router error 503")
    err.status, err.detail = 503, LOCAL_503
    return err


def _r4_router(monkeypatch, hold: asyncio.Event | None = None):
    """A router fake with the real rules that matter here: a local pick 503s (backend down);
    no engine on an unmapped thread is recorded as classify. Returns (routed, asked)."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {})  # readable, nothing mapped: message 1 never made a row
    _seed_local_engine(monkeypatch)
    routed: list[tuple] = []
    asked: list[str] = []

    async def fake_ask(q, brain="auto", codes=False):
        asked.append(q)
        return quick.QuickAnswer("quick", "web", 1, None)

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append((text, engine))
        if engine != LOCAL_ENGINE:
            return {"action": "spawned", "session_id": "s-cl", "kind": "claude", "title": "classified"}
        if hold is not None and text == "SECRET_M1":
            await hold.wait()
        raise _local_503()

    monkeypatch.setattr(quick, "ask", fake_ask)
    monkeypatch.setattr(central, "route", route)
    return routed, asked


def test_siri_r4_followups_in_flight_then_message1_503(monkeypatch, stub_router):
    """R4-1 INFLIGHT503: follow-ups queue behind a local message 1 that then gets a 503.
    They must reach the router on the local engine, never as a no-engine (classify) call."""
    hold = asyncio.Event()
    routed, asked = _r4_router(monkeypatch, hold)

    async def scenario():
        from gateway.app import app as gw_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw_app), base_url="http://t") as c:
            t1 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "SECRET_M1", "thread_id": "thr-r4a", "engine": LOCAL_ENGINE}))
            for _ in range(100):
                if routed:
                    break
                await asyncio.sleep(0.01)
            t2 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "? SECRET_R4_FQ", "thread_id": "thr-r4a"}))
            t3 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "SECRET_R4_FP", "thread_id": "thr-r4a", "engine": "hub"}))
            await asyncio.sleep(0.05)
            hold.set()
            return await asyncio.gather(t1, t2, t3)

    responses = asyncio.run(scenario())
    assert asked == [], f"leak: quick.ask got {asked}"
    assert routed == [("SECRET_M1", LOCAL_ENGINE), ("? SECRET_R4_FQ", LOCAL_ENGINE),
                      ("SECRET_R4_FP", LOCAL_ENGINE)], f"leak: {routed}"
    assert [r.status_code for r in responses] == [503, 503, 503]
    assert responses[0].json()["detail"] == LOCAL_503


def test_siri_r4_message1_busy_then_followup(monkeypatch, stub_router):
    """R4-1 INFLIGHTBUSY: message 1 gets the busy reply (no router row); the follow-up
    with no engine still goes out on the local engine."""
    routed, asked = _r4_router(monkeypatch)
    slots = iter([False])
    monkeypatch.setattr(siri, "_brain_slot_available", lambda: next(slots, True))
    resp = _call("POST", "/siri/hey", headers=IOS,
                 json={"text": "SECRET_M1", "thread_id": "thr-r4b", "engine": LOCAL_ENGINE})
    assert resp.json() == {"reply": siri._BUSY_REPLY} and routed == []
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "SECRET_R4_BP", "thread_id": "thr-r4b"})
    assert routed == [("SECRET_R4_BP", LOCAL_ENGINE)], f"leak: {routed}"
    assert resp.status_code == 503 and asked == []


def test_siri_r4_timeout_late_503_note_then_followup(monkeypatch, stub_router):
    """R4-1 timeout then late 503: the owner saw "Still thinking"; the late 503 gets one
    fixed note (no text), and a later no-engine follow-up still goes out on the local engine."""
    hold = asyncio.Event()
    routed, asked = _r4_router(monkeypatch, hold)
    # the threaded path uses _router_reply's timeout; shorten it for this test only
    # (Mac line: _router_reply has no timeout parameter, it reads ROUTER_TIMEOUT_SECONDS)
    monkeypatch.setattr(siri, "ROUTER_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(siri, "LATE_ANSWER_MAX_SECONDS", 0.3)
    monkeypatch.setattr(siri, "LATE_RETRY_SECONDS", 0.01)
    sent: list[tuple] = []

    async def app_push(kind, title, text, session_id=None):
        sent.append(("app", title, text))
        return True

    async def wa(text, subject):
        sent.append(("wa", subject, text))
        return True

    monkeypatch.setattr(notify, "push_app", app_push)
    monkeypatch.setattr(notify, "whatsapp_owner", wa)

    async def scenario():
        from gateway.app import app as gw_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw_app), base_url="http://t") as c:
            before = set(siri._late_tasks)
            r1 = await c.post("/siri/hey", headers=IOS, json={
                "text": "SECRET_M1", "thread_id": "thr-r4c", "engine": LOCAL_ENGINE})
            hold.set()
            await asyncio.wait_for(asyncio.gather(*(siri._late_tasks - before)), 5)
            r2 = await c.post("/siri/hey", headers=IOS, json={"text": "SECRET_R4_HP", "thread_id": "thr-r4c"})
            return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1.json() == {"reply": siri._TIMEOUT_REPLY}
    note = "The local model could not answer. Nothing was sent anywhere else."
    assert sent == [("app", "Local chat", note), ("wa", "[jarvis] Local chat", note)], sent
    assert routed[-1] == ("SECRET_R4_HP", LOCAL_ENGINE), f"leak: {routed}"
    assert all(e == LOCAL_ENGINE for _, e in routed) and asked == [] and r2.status_code == 503


def test_siri_r4_cloud_custom_engine_mark_cleared(monkeypatch, stub_router):
    """R4 take-if-small: an early mark on a listed cloud custom engine is cleared when the
    router reply shows that cloud kind, so the thread keeps its `?` quick path."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {})
    monkeypatch.setattr(central, "_engines_cache",
                        {"at": time.monotonic(), "ids": frozenset({LOCAL_ENGINE, "cloudx"}),
                         "local": frozenset({LOCAL_ENGINE})})
    asked: list[str] = []

    async def fake_ask(q, brain="auto", codes=False):
        asked.append(q)
        return quick.QuickAnswer("quick", "web", 1, None)

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        return {"action": "spawned", "session_id": "s-cx", "kind": engine or "claude", "title": "t"}

    monkeypatch.setattr(quick, "ask", fake_ask)
    monkeypatch.setattr(siri, "roles", lambda: [])
    monkeypatch.setattr(central, "route", route)
    resp = _call("POST", "/siri/hey", headers=IOS,
                 json={"text": "hi", "thread_id": "thr-cx", "engine": "cloudx"})
    assert resp.status_code == 200, resp.text
    assert "thr-cx" not in siri._LOCAL_THREADS
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "? what is x", "thread_id": "thr-cx"})
    assert resp.status_code == 200 and asked == ["what is x"]


def test_siri_r4_missing_cards_db_fails_closed(monkeypatch, stub_router):
    """R4 take-if-small: a gateway copy with no cards._db at all (the Mac) is "cannot tell":
    the request fails closed (no quick.ask), never a 500."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    monkeypatch.delattr(siri.cards, "_db")
    routed: list[str] = []

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append(text)
        return {"action": "answered", "session_id": None, "reply": "ok"}

    monkeypatch.setattr(central, "route", route)
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "? SECRET_R4_DB", "thread_id": "thr-nodb"})
    assert resp.status_code == 200, resp.text
    assert routed == ["? SECRET_R4_DB"]


def test_deliver_late_cloud_reply_keeps_newer_local_mark(monkeypatch):
    """R4: a late Hub reply (kind claude) lands after a newer local request marked the
    thread. The late path runs outside the thread gate, so it must not clear that mark."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    siri._mark_local("thr-old", LOCAL_ENGINE)
    sent: list[tuple] = []

    async def app_push(kind, title, text, session_id=None):
        sent.append(title)
        return True

    async def wa(text, subject):
        sent.append(subject)
        return True

    monkeypatch.setattr(notify, "push_app", app_push)
    monkeypatch.setattr(notify, "whatsapp_owner", wa)

    async def late():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "spawned", "session_id": "s-cl", "kind": "claude", "title": "Hub chat"}))
        await siri._deliver_late(task, "older hub message", "thr-old", "m0")

    asyncio.run(late())
    assert siri._LOCAL_THREADS == {"thr-old": LOCAL_ENGINE}


def test_siri_r4_invalid_engine_id_not_resent(monkeypatch, stub_router):
    """R4 (security finding 2): a malformed engine id gets its 400 once; a follow-up
    naming no engine is not re-sent with it (no sticky 400), and stays off quick.ask."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _router_db(monkeypatch, {})
    _seed_local_engine(monkeypatch)
    routed: list = []

    async def route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        routed.append(engine)
        return {"action": "answered", "session_id": None, "reply": "ok"}

    monkeypatch.setattr(central, "route", route)
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "a", "thread_id": "thr-bad", "engine": "bad id!"})
    assert resp.status_code == 400
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "? b", "thread_id": "thr-bad"})
    assert resp.status_code == 200 and routed == [None], (resp.text, routed)


def test_siri_r5_queued_followup_keeps_local_engine_over_later_custom_pick(monkeypatch, stub_router):
    """R5-1 OVERWRITE_QUEUED: m1 local held, m2 no engine queued, m3 names a listed non-local
    custom engine, then m1 503s. m3's early mark must not replace the stored local engine,
    so m2 goes out on it and never reaches classify."""
    hold = asyncio.Event()
    routed, asked = _r4_router(monkeypatch, hold)
    monkeypatch.setattr(central, "_engines_cache",
                        {"at": time.monotonic(), "ids": frozenset({LOCAL_ENGINE, "cloudx"}),
                         "local": frozenset({LOCAL_ENGINE})})

    async def scenario():
        from gateway.app import app as gw_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw_app), base_url="http://t") as c:
            t1 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "SECRET_M1", "thread_id": "thr-r5", "engine": LOCAL_ENGINE}))
            for _ in range(100):
                if routed:
                    break
                await asyncio.sleep(0.01)
            t2 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "SECRET_M2", "thread_id": "thr-r5"}))
            await asyncio.sleep(0.02)
            t3 = asyncio.create_task(c.post("/siri/hey", headers=IOS, json={
                "text": "PICK_M3", "thread_id": "thr-r5", "engine": "cloudx"}))
            await asyncio.sleep(0.05)
            hold.set()
            return await asyncio.gather(t1, t2, t3)

    responses = asyncio.run(scenario())
    assert asked == [], f"leak: quick.ask got {asked}"
    assert routed == [("SECRET_M1", LOCAL_ENGINE), ("SECRET_M2", LOCAL_ENGINE),
                      ("PICK_M3", "cloudx")], f"leak: {routed}"
    assert [r.status_code for r in responses] == [503, 503, 200]


def test_deliver_late_local_reply_keeps_newer_stored_engine(monkeypatch):
    """R5 sweep: a late reply for an older no-engine request on a local thread lands after a
    newer request stored `local-qwen`. It must not replace that engine with None, so a queued
    no-engine follow-up still goes out on the local engine."""
    monkeypatch.setattr(siri, "_LOCAL_THREADS", {})
    _seed_local_engine(monkeypatch)
    siri._mark_local("thr-late", LOCAL_ENGINE)

    async def quiet(*a, **kw):
        return True

    monkeypatch.setattr(notify, "push_app", quiet)
    monkeypatch.setattr(notify, "whatsapp_owner", quiet)

    async def late():
        task = asyncio.ensure_future(asyncio.sleep(0, result={
            "action": "answered", "session_id": "s-loc", "upstream": "mini-pc", "reply": "x"}))
        await siri._deliver_late(task, "older message", "thr-late", "m0", None, False, True, False)

    asyncio.run(late())
    assert siri._LOCAL_THREADS == {"thr-late": LOCAL_ENGINE}


def test_siri_r5_malformed_engine_keeps_stored_local_engine(monkeypatch, stub_router):
    """R5 sweep: m1 local 503s, m2 names a malformed engine id (400), m3 names no engine.
    The G2 mark must not store the malformed id over `local-qwen`, so m3 stays local."""
    routed, asked = _r4_router(monkeypatch)
    r1 = _call("POST", "/siri/hey", headers=IOS,
               json={"text": "SECRET_M1", "thread_id": "thr-r5b", "engine": LOCAL_ENGINE})
    r2 = _call("POST", "/siri/hey", headers=IOS,
               json={"text": "SECRET_M2", "thread_id": "thr-r5b", "engine": "bad id!"})
    r3 = _call("POST", "/siri/hey", headers=IOS, json={"text": "SECRET_M3", "thread_id": "thr-r5b"})
    assert [r1.status_code, r2.status_code, r3.status_code] == [503, 400, 503]
    assert routed == [("SECRET_M1", LOCAL_ENGINE), ("SECRET_M3", LOCAL_ENGINE)], f"leak: {routed}"
    assert asked == []


def test_siri_r6_status_phrase_local_pick_replaces_stored_custom_engine(monkeypatch, stub_router):
    """R6-1: m0 names a listed non-local custom engine and gets busy (no row), m1 picks local
    with a status phrase, m2 names no engine. The local pick is the latest one, so m2 must go
    out on the local engine, never to classify."""
    routed, asked = _r4_router(monkeypatch)
    monkeypatch.setattr(central, "_engines_cache",
                        {"at": time.monotonic(), "ids": frozenset({LOCAL_ENGINE, "cloudx"}),
                         "local": frozenset({LOCAL_ENGINE})})
    slots = iter([False])
    monkeypatch.setattr(siri, "_brain_slot_available", lambda: next(slots, True))
    r0 = _call("POST", "/siri/hey", headers=IOS,
               json={"text": "PICK_M0", "thread_id": "thr-r6a", "engine": "cloudx"})
    assert r0.json() == {"reply": siri._BUSY_REPLY} and routed == []
    r1 = _call("POST", "/siri/hey", headers=IOS,
               json={"text": "are you there", "thread_id": "thr-r6a", "engine": LOCAL_ENGINE})
    assert r1.status_code == 200 and routed == []
    r2 = _call("POST", "/siri/hey", headers=IOS, json={"text": "SECRET_M2", "thread_id": "thr-r6a"})
    assert routed == [("SECRET_M2", LOCAL_ENGINE)], f"leak: {routed}"
    assert r2.status_code == 503 and asked == []


def test_siri_r6_too_long_first_local_message_marks_thread(monkeypatch, stub_router):
    """R6-2: a first local message over MAX_TEXT_CHARS gets its 400 but still marks the thread,
    so a no-engine follow-up and a `?` follow-up both go out on the local engine."""
    routed, asked = _r4_router(monkeypatch)
    r1 = _call("POST", "/siri/hey", headers=IOS, json={
        "text": "S" * (siri.MAX_TEXT_CHARS + 1), "thread_id": "thr-r6b", "engine": LOCAL_ENGINE})
    assert r1.status_code == 400 and routed == []
    r2 = _call("POST", "/siri/hey", headers=IOS, json={"text": "SECRET_M2", "thread_id": "thr-r6b"})
    r3 = _call("POST", "/siri/hey", headers=IOS, json={"text": "? SECRET_M3", "thread_id": "thr-r6b"})
    assert routed == [("SECRET_M2", LOCAL_ENGINE), ("? SECRET_M3", LOCAL_ENGINE)], f"leak: {routed}"
    assert [r2.status_code, r3.status_code] == [503, 503] and asked == []


# --- G1 (round 7): /central/sessions/{id}/message forwards `engine`; the router checks it ---

def _recording_router(monkeypatch: pytest.MonkeyPatch, reply):
    """Fake _router_json: records every call; `reply(path, body)` returns or raises."""
    calls: list[tuple[str, str, dict | None]] = []

    async def fake(method, path, body=None):
        calls.append((method, path, body))
        return reply(path, body)

    monkeypatch.setattr(central, "_router_json", fake)
    return calls


def test_session_message_forwards_engine_only_when_sent(monkeypatch):
    """written by builder (round 7, G1)."""
    calls = _recording_router(monkeypatch, lambda path, body: {"ok": True, "action": "queued"})
    for extra, want in (({"engine": LOCAL_ENGINE}, {"engine": LOCAL_ENGINE}),
                        ({"engine": None}, {"engine": None}),  # explicit null reaches the router
                        ({}, {})):  # not sent: the router keeps its old behaviour
        resp = _call("POST", "/central/sessions/s-loc/message", headers=IOS,
                     json={"msg_id": "m-g1", "text": "hi", **extra})
        assert resp.status_code == 200, resp.text
        assert calls[-1] == ("POST", "/sessions/s-loc/message", {"msg_id": "m-g1", "text": "hi", **want})
    bad = _call("POST", "/central/sessions/s-loc/message", headers=IOS,
                json={"msg_id": "m-g1", "text": "hi", "engine": "Not An Id"})
    assert bad.status_code == 422 and len(calls) == 3


def test_session_message_engine_409_passthrough_never_rerouted(monkeypatch):
    """written by builder (round 7, G1): the router's engine 409 reaches the client verbatim,
    and a glm row's 409 fallback never re-routes the text without its engine."""
    # Mac line: no glm 409 re-route (cards.glm_thread_of) exists; set it anyway so a future port is still covered
    monkeypatch.setattr(central.cards, "glm_thread_of", lambda sid: "glm-thread", raising=False)
    detail = "This chat belongs to GLM. Start a new chat to use the local model."

    def reply(path, body):
        assert path == "/sessions/s-glm/message", f"re-routed: {path}"
        err = central.RouterError("router error 409")
        err.status, err.detail = 409, detail
        raise err

    calls = _recording_router(monkeypatch, reply)
    resp = _call("POST", "/central/sessions/s-glm/message", headers=IOS,
                 json={"msg_id": "m-g1b", "text": "SECRET local text", "engine": LOCAL_ENGINE})
    assert resp.status_code == 409 and resp.json()["detail"] == detail
    assert [c[1] for c in calls] == ["/sessions/s-glm/message"]


def test_chat_body_engine_still_validated(monkeypatch):
    """written by builder (round 7): ChatBody now inherits `engine` from MessageBody."""
    resp = _call("POST", "/chat", headers=IOS,
                 json={"msg_id": "m-c", "thread_id": "t-c", "text": "hi", "engine": "Bad Id"})
    assert resp.status_code == 422
