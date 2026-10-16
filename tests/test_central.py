"""Central chat proxy (gateway/central.py) + the /siri/hey router fallback.

The upstream mini PC router is gateway/stubs/router_stub.py mounted through
httpx.ASGITransport — no network. `common.config.get_token` is stubbed
name-aware (shared / ios / router tokens are fixtures) so the vault is never
read; ROUTER_SECRET must never surface in a response or a log line.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
import pytest

from common import config
from gateway import central
from gateway.stubs import router_stub

TOKEN = "jarvis_test_token_123"
IOS_TOKEN = "jarvis_ios_test_token_456"
ROUTER_SECRET = "router_secret_value_789"
IOS = {"Authorization": f"Bearer {IOS_TOKEN}"}


@pytest.fixture(autouse=True)
def stub_router(monkeypatch: pytest.MonkeyPatch):
    tokens = {"token": TOKEN, "ios_token": IOS_TOKEN, central.ROUTER_TOKEN_NAME: ROUTER_SECRET}
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": tokens.get(name))
    monkeypatch.setattr(central, "router_base", lambda: "http://router.test")
    # central's outbound client talks to the stub router in-process.
    monkeypatch.setattr(
        central, "_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.ASGITransport(app=router_stub.app), timeout=timeout),
    )
    router_stub.seen.clear()
    monkeypatch.setattr(router_stub, "fail_status", None)
    monkeypatch.setattr(router_stub, "watchdog_available", False)

    import collections

    from gateway import siri

    monkeypatch.setattr(siri, "_brain_call_times", collections.deque())
    monkeypatch.setattr(siri, "_brain_semaphore", asyncio.Semaphore(2))
    return tokens


def _call(method: str, path: str, headers: dict | None = None, **kw) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.request(method, path, headers=headers, **kw)

    return asyncio.run(run())


ENDPOINTS = [
    ("POST", "/chat", {"thread_id": "app-1", "msg_id": "m-1", "text": "hi"}),
    ("GET", "/central/sessions", None),
    ("GET", "/central/sessions/s-run/events", None),
    ("POST", "/central/sessions/s-run/message", {"msg_id": "m-2", "text": "yes"}),
    ("POST", "/central/sessions/s-run/stop", None),
    ("GET", "/central/feed", None),
]


# --- auth ----------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_no_token_is_401(method, path, body):
    assert _call(method, path, json=body).status_code == 401
    assert router_stub.seen == []


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_shared_token_is_401(method, path, body):
    resp = _call(method, path, headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
    assert resp.status_code == 401
    assert router_stub.seen == []


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_ios_token_is_200(method, path, body):
    resp = _call(method, path, headers=IOS, json=body)
    assert resp.status_code == 200, resp.text
    assert router_stub.seen[-1]["headers"]["authorization"] == f"Bearer {ROUTER_SECRET}"


# --- allowlist -----------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("POST", "/route"),
    ("GET", "/central/health"),
    ("POST", "/central/route"),
    ("GET", "/central/sessions/s-run"),
    ("POST", "/central/sessions/s-run/kill"),
    ("GET", "/central/sessions/s-run/events/extra"),
    ("GET", "/central/anything/at/all"),
])
def test_unknown_paths_404(method, path):
    assert _call(method, path, headers=IOS, json={}).status_code == 404
    assert router_stub.seen == []


@pytest.mark.parametrize("body", [
    {"thread_id": "a b", "msg_id": "m", "text": "hi"},
    {"thread_id": "../etc", "msg_id": "m", "text": "hi"},
    {"thread_id": "t" * 65, "msg_id": "m", "text": "hi"},
    {"thread_id": "t", "msg_id": "m/1", "text": "hi"},
    {"thread_id": "t", "msg_id": "m", "text": ""},
    {"thread_id": "t", "msg_id": "m", "text": "x" * 4001},
    {"msg_id": "m", "text": "hi"},
])
def test_chat_body_validation_422(body):
    assert _call("POST", "/chat", headers=IOS, json=body).status_code == 422
    assert router_stub.seen == []


@pytest.mark.parametrize("sid", ["s_run", "s.run", "s%20run", "x" * 65])
def test_session_id_validated(sid):
    assert _call("POST", f"/central/sessions/{sid}/stop", headers=IOS).status_code in (404, 422)
    assert router_stub.seen == []


# --- forwarding ----------------------------------------------------------------

def test_chat_forwards_with_source_app_and_filters_keys():
    resp = _call("POST", "/chat", headers=IOS, json={"thread_id": "app-1a2b", "msg_id": "m-1", "text": "do a thing"})
    assert resp.json() == {"action": "spawned", "session_id": "s-new", "kind": "claude",
                           "title": "New task", "reply": "Started", "seq": 3}
    sent = router_stub.seen[-1]
    assert sent["path"] == "/route"
    assert sent["body"] == {"thread_id": "app-1a2b", "msg_id": "m-1", "text": "do a thing", "source": "app"}


def test_chat_answered_passes_null_session_id():
    resp = _call("POST", "/chat", headers=IOS, json={"thread_id": "app-1", "msg_id": "m-1", "text": "tell me x"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "answered"
    assert "session_id" in data and data["session_id"] is None
    assert data["reply"] == "42"


def test_chat_needs_input_carries_prompt_and_options():
    resp = _call("POST", "/chat", headers=IOS, json={"thread_id": "t", "msg_id": "m", "text": "ask me"})
    assert resp.json()["prompt"] == "Which env?"
    assert resp.json()["options"] == ["staging", "prod"]


def test_sessions_filtered_with_phase_fallback():
    data = _call("GET", "/central/sessions", headers=IOS).json()
    assert [s["phase"] for s in data] == ["in_progress", "in_review", "done"]
    assert data[0]["task"] == "Fix login bug"
    assert "internal" not in data[1]


def test_phase_fallback_map(monkeypatch):
    states = ["running", "needs_input", "done", "failed", "idle", "stopped", "weird", None]
    monkeypatch.setattr(router_stub, "SESSIONS", [
        {"session_id": f"s{i}", "state": st, "phase": "bogus"} for i, st in enumerate(states)
    ] + [{"session_id": "live", "state": "done", "phase": "in_review"}])
    data = _call("GET", "/central/sessions", headers=IOS).json()
    assert [s["phase"] for s in data] == [
        "in_progress", "in_review", "done", "done", "done", "done", "in_progress", "in_progress",
        "in_review",  # a valid router phase wins over the state map
    ]


def test_message_and_stop_forward():
    resp = _call("POST", "/central/sessions/s-run/message", headers=IOS, json={"msg_id": "m-9", "text": "go"})
    assert resp.json() == {"ok": True, "action": "delivered"}
    assert router_stub.seen[-1]["path"] == "/sessions/s-run/message"
    assert router_stub.seen[-1]["body"] == {"msg_id": "m-9", "text": "go"}
    assert _call("POST", "/central/sessions/s-run/stop", headers=IOS).json() == {"ok": True}
    assert router_stub.seen[-1]["path"] == "/sessions/s-run/stop"


# --- SSE passthrough -------------------------------------------------------------

def test_events_sse_passthrough_with_last_event_id():
    resp = _call("GET", "/central/sessions/s-run/events", headers={**IOS, "Last-Event-ID": "5"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == router_stub.sse_body("s-run", 5)
    assert router_stub.seen[-1]["headers"]["last-event-id"] == "5"
    assert re.findall(r"^id: (\d+)$", resp.text, re.M) == ["6", "7"]


def test_events_bad_last_event_id_not_forwarded():
    resp = _call("GET", "/central/sessions/s-run/events", headers={**IOS, "Last-Event-ID": "5abc"})
    assert resp.status_code == 200
    assert "last-event-id" not in router_stub.seen[-1]["headers"]


def test_feed_sse_passthrough():
    resp = _call("GET", "/central/feed", headers=IOS)
    assert resp.status_code == 200
    assert '"type": "spawn"' in resp.text
    assert router_stub.seen[-1]["path"] == "/feed"


# --- /central/watchdog (router session ce03d92a; not live yet) -------------------

def test_watchdog_unavailable_when_router_404s():
    resp = _call("GET", "/central/watchdog", headers=IOS)
    assert resp.status_code == 200
    assert resp.json() == {
        "available": False, "stale": None, "age_s": None,
        "last_sweep_at": None, "sweep_errors_last_hour": None, "alerts": [],
    }


def test_watchdog_down_is_also_unavailable(monkeypatch):
    monkeypatch.setattr(router_stub, "fail_status", 500)
    resp = _call("GET", "/central/watchdog", headers=IOS)
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_watchdog_available_and_fresh(monkeypatch):
    monkeypatch.setattr(router_stub, "watchdog_available", True)
    now = datetime(2026, 10, 16, 10, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(central, "_now", lambda: now)
    resp = _call("GET", "/central/watchdog", headers=IOS)
    body = resp.json()
    assert body["available"] is True
    assert body["last_sweep_at"] == router_stub.WATCHDOG["last_sweep_at"]
    assert body["age_s"] == pytest.approx(120.0)
    assert body["stale"] is False


def test_watchdog_available_but_stale(monkeypatch):
    monkeypatch.setattr(router_stub, "watchdog_available", True)
    now = datetime(2026, 10, 16, 10, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(central, "_now", lambda: now)
    resp = _call("GET", "/central/watchdog", headers=IOS)
    body = resp.json()
    assert body["available"] is True
    assert body["age_s"] == pytest.approx(600.0)
    assert body["stale"] is True


def test_watchdog_needs_ios_token():
    assert _call("GET", "/central/watchdog").status_code == 401


# --- router failures -------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_router_error_is_generic_502(method, path, body, monkeypatch):
    monkeypatch.setattr(router_stub, "fail_status", 500)
    resp = _call(method, path, headers=IOS, json=body)
    assert resp.status_code == 502
    assert resp.json() == {"detail": "router error 500"}
    assert router_stub.FAIL_BODY not in resp.text


def test_router_unreachable_is_502(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        central, "_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=timeout),
    )
    for method, path, body in ENDPOINTS:
        resp = _call(method, path, headers=IOS, json=body)
        assert resp.status_code == 502
        assert resp.json() == {"detail": "router unavailable"}


@pytest.mark.parametrize("path", ["/central/sessions/s-run/events", "/central/feed"])
def test_sse_unexpected_error_is_502_and_client_closed(path, monkeypatch):
    closed: list[bool] = []

    class Boom(httpx.AsyncClient):
        async def send(self, *a, **kw):
            raise RuntimeError("not an httpx error")

        async def aclose(self):
            closed.append(True)
            await super().aclose()

    monkeypatch.setattr(central, "_client", lambda timeout: Boom(timeout=timeout))
    resp = _call("GET", path, headers=IOS)
    assert resp.status_code == 502
    assert resp.json() == {"detail": "router unavailable"}
    assert closed == [True]


def test_router_token_missing_is_502(stub_router):
    stub_router[central.ROUTER_TOKEN_NAME] = None
    resp = _call("GET", "/central/sessions", headers=IOS)
    assert resp.status_code == 502
    assert resp.json() == {"detail": "router not configured"}
    assert router_stub.seen == []


def test_router_token_never_in_responses_or_logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    responses = [_call(m, p, headers=IOS, json=b) for m, p, b in ENDPOINTS]
    responses.append(_call("POST", "/siri/hey", headers=IOS, json={"text": "do a thing"}))
    monkeypatch.setattr(router_stub, "fail_status", 503)
    responses += [_call(m, p, headers=IOS, json=b) for m, p, b in ENDPOINTS]
    for resp in responses:
        assert ROUTER_SECRET not in resp.text
        assert ROUTER_SECRET not in str(resp.headers)
    assert caplog.records, "expected some log output to check"
    assert ROUTER_SECRET not in caplog.text


# --- /siri/hey fallback -> router ---------------------------------------------------

def test_siri_fallback_routes_to_router():
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "build me a thing"})
    assert resp.status_code == 200
    assert resp.json() == {"action": "spawned", "session_id": "s-new", "kind": "claude",
                           "title": "New task", "reply": "Started"}
    sent = router_stub.seen[-1]
    assert sent["path"] == "/route"
    assert sent["body"]["source"] == "voice"
    assert sent["body"]["thread_id"] == "phone-voice"
    assert re.fullmatch(r"[0-9a-f]{32}", sent["body"]["msg_id"])


def test_siri_fallback_answered_inline_with_client_thread():
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "tell me the time", "thread_id": "app-xyz"})
    data = resp.json()
    assert data["reply"] == "42"
    assert data["action"] == "answered"
    assert "session_id" in data and data["session_id"] is None
    assert router_stub.seen[-1]["body"]["thread_id"] == "app-xyz"


def test_siri_shared_token_never_reaches_router(monkeypatch):
    from gateway import siri

    async def must_not_route(*args):
        raise AssertionError("shared token must never reach central.route")

    monkeypatch.setattr(central, "route", must_not_route)
    monkeypatch.setattr(siri, "roles", lambda: [])
    shared = {"Authorization": f"Bearer {TOKEN}"}
    for text in ("tell me the time", "build me a thing", "ask me something"):
        resp = _call("POST", "/siri/hey", headers=shared, json={"text": text, "thread_id": "app-xyz"})
        assert resp.status_code == 200
        assert resp.json() == {"reply": "Tasks go through the Jarvis app"}
    # Local intents still answer for the Shortcut.
    resp = _call("POST", "/siri/hey", headers=shared, json={"text": "status"})
    assert resp.json() == {"reply": "Jarvis gateway online. Connected: none"}
    assert router_stub.seen == []


def test_siri_fallback_needs_input_speaks_prompt():
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "ask me something"})
    assert resp.json()["reply"] == "Which env?"
    assert resp.json()["options"] == ["staging", "prod"]


def test_siri_client_msg_id_passed_to_router():
    _call("POST", "/siri/hey", headers=IOS, json={"text": "build me a thing", "msg_id": "m-abc-1"})
    assert router_stub.seen[-1]["body"]["msg_id"] == "m-abc-1"


def test_siri_bad_msg_id_400():
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "hello", "msg_id": "m/1"})
    assert resp.status_code == 400
    assert router_stub.seen == []


def test_siri_text_over_4000_is_400_before_intents(monkeypatch):
    from gateway import siri

    monkeypatch.setattr(siri, "roles", lambda: [])
    for headers in (IOS, {"Authorization": f"Bearer {TOKEN}"}):
        resp = _call("POST", "/siri/hey", headers=headers, json={"text": "status " + "x" * 4000})
        assert resp.status_code == 400
        resp = _call("POST", "/siri/hey", headers=headers, json={"text": "q: " + "x" * 4000})
        assert resp.status_code == 400
    assert _call("POST", "/siri/hey", headers=IOS, json={"text": "x" * 4000}).status_code == 200
    assert len(router_stub.seen) == 1


def test_siri_bad_thread_id_400():
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "hello", "thread_id": "../x"})
    assert resp.status_code == 400
    assert router_stub.seen == []


def test_siri_router_down_canned_reply(monkeypatch):
    monkeypatch.setattr(router_stub, "fail_status", 500)
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "The mini PC router is unavailable, try again."}


def test_siri_status_stays_local(monkeypatch):
    from gateway import siri

    monkeypatch.setattr(siri, "roles", lambda: [])
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "status"})
    assert resp.json() == {"reply": "Jarvis gateway online. Connected: none"}
    assert router_stub.seen == []


# --- screen_minipc intent -------------------------------------------------------------

@pytest.mark.parametrize("text,cmd", [
    ("show mini pc screen", "screen_minipc"),
    ("Mini PC screen", "screen_minipc"),
    ("show my mini pc", None),  # no "screen": not a screen request
    ("Show Mini PC screen", "screen_minipc"),
    ("mini-pc screen", "screen_minipc"),
    ("show the minipc screen", "screen_minipc"),
    ("open the mini pc's screen", "screen_minipc"),
    ("show me the mini pc logs", None),
    ("show mini pc disk usage", "disk"),
    ("what is the mini pc showing", None),
    ("show my pc screen", "screen_windows"),
    ("show my mac screen", "screen_mac"),
    ("is the mini pc busy", None),
])
def test_voice_router_minipc_intent(text, cmd):
    from mac.voice import router as voice_router

    assert voice_router.route(text) == cmd


def test_siri_screen_minipc_launches(monkeypatch):
    from gateway import screen

    launches: list[str] = []

    async def fake_launch(target):
        launches.append(target)

    monkeypatch.setattr(screen, "launch_screen", fake_launch)
    resp = _call("POST", "/siri/hey", headers=IOS, json={"text": "Show Mini PC screen"})
    assert resp.json() == {"reply": "Opening your Mini PC screen"}
    assert launches == ["minipc"]
    assert router_stub.seen == []
