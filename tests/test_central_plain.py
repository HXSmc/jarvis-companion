"""Round 3 plain words on /central/sessions: device, doing/recap cache, clear-done, floor idle.

Reuses test_central's stub router (autouse fixture imported below). conftest
points STATE_PATH at tmp_path and stubs `_generate_plain`, so no test reads the
vault, writes ~/.jarvis, or calls a model.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from datetime import datetime, timezone

import pytest

from gateway import central
from gateway.stubs import router_stub
from test_central import IOS, TOKEN, _call, stub_router  # noqa: F401  (autouse fixture)

REAL_GENERATE = central._generate_plain  # captured before conftest stubs it
NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def _sessions(monkeypatch, items, now=NOW):
    monkeypatch.setattr(router_stub, "SESSIONS", items)
    monkeypatch.setattr(central, "_now", lambda: now)
    resp = _call("GET", "/central/sessions", headers=IOS)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cache(sid, summary, doing, recap):
    central._save_state({"cleared_before": None, "plain": {
        sid: {"hash": central._summary_hash(summary), "doing": doing, "recap": recap},
    }})


# --- device / doing / recap ---------------------------------------------------

def test_device_and_title_fallback_without_cache():
    data = _call("GET", "/central/sessions", headers=IOS).json()
    assert {s["device"] for s in data} == {"Mini PC"}
    assert [s["doing"] for s in data] == ["Fix login", "Deploy", "Notes"]
    assert all(s["recap"] == [] for s in data)


def test_cache_hit_gives_doing_and_recap():
    _cache("s-run", "Patching auth", "Fixing the login page", ["I fixed the password check"])
    run = _call("GET", "/central/sessions", headers=IOS).json()[0]
    assert run["doing"] == "Fixing the login page"
    assert run["recap"] == ["I fixed the password check"]


def test_poll_never_waits_on_the_model(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def slow(session):
        started.set()
        release.wait(5)  # a hung model call
        return None

    monkeypatch.setattr(central, "_generate_plain", slow)

    async def run():
        from gateway.app import app
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            t0 = time.monotonic()
            data = (await c.get("/central/sessions", headers=IOS)).json()
            elapsed = time.monotonic() - t0
            asked = await asyncio.to_thread(started.wait, 2)
            release.set()
            await asyncio.gather(*list(central._tasks))
            return data, elapsed, asked

    data, elapsed, asked = asyncio.run(run())
    assert asked  # the model was asked, in the background
    assert elapsed < 2  # while the reply did not wait for it
    assert data[0]["doing"] == "Fix login"  # answered from the router title


def test_background_worker_fills_cache_once(monkeypatch):
    seen = []

    def gen(session):
        seen.append(session["session_id"])
        return {"doing": "Fixing the login page", "recap": ["I fixed it"]}

    monkeypatch.setattr(central, "_generate_plain", gen)

    async def run():
        # Same loop for the poll and the worker, then let the worker finish.
        from gateway.app import app
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            await c.get("/central/sessions", headers=IOS)
            await asyncio.gather(*list(central._tasks))
            await c.get("/central/sessions", headers=IOS)  # cached now: no second call
            return (await c.get("/central/sessions", headers=IOS)).json()

    data = asyncio.run(run())
    assert seen == ["s-run"]  # only s-run has a summary
    assert data[0]["doing"] == "Fixing the login page"
    assert data[0]["recap"] == ["I fixed it"]
    assert stat.S_IMODE(os.stat(central.STATE_PATH).st_mode) == 0o600


def test_failed_generation_not_retried_each_poll(monkeypatch):
    seen = []

    def gen(session):
        seen.append(1)
        return None

    monkeypatch.setattr(central, "_generate_plain", gen)

    async def run():
        from gateway.app import app
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            for _ in range(3):
                await c.get("/central/sessions", headers=IOS)
                await asyncio.gather(*list(central._tasks))

    asyncio.run(run())
    assert seen == [1]


def test_clean_plain_validates_model_reply():
    text = 'Sure!\n```json\n{"doing": "**Fixing** the login page", "recap": ["- I fixed it", "", 5, "' + "x" * 300 + '"]}\n```'
    out = central._clean_plain(text)
    assert out["doing"] == "Fixing the login page"
    assert out["recap"][0] == "I fixed it"
    assert len(out["recap"]) == 2 and len(out["recap"][1]) == central.RECAP_ITEM_MAX
    assert central._clean_plain("no json here") is None
    assert central._clean_plain('{"recap": ["x"]}') is None
    assert len(central._clean_plain(json.dumps({"doing": "y" * 200}))["doing"]) == central.DOING_MAX
    many = central._clean_plain(json.dumps({"doing": "d", "recap": [f"I did {i}" for i in range(10)]}))
    assert len(many["recap"]) == central.RECAP_MAX_ITEMS


def test_generate_plain_payload(monkeypatch):
    from gateway import quick

    monkeypatch.setattr(quick, "_settings", lambda: {"openrouter_key": "k", "summary_model": "m/x"})
    sent = {}

    def fake(url, payload, *, key, timeout_s):
        sent.update(url=url, payload=payload, key=key)
        return {"choices": [{"message": {"content": '{"doing": "Fixing login", "recap": ["I did it"]}'}}]}

    monkeypatch.setattr(quick, "_openrouter", fake)
    out = REAL_GENERATE({"session_id": "s", "title": "Fix login", "summary": "Patching auth"})
    assert out == {"doing": "Fixing login", "recap": ["I did it"]}
    assert sent["url"] == quick._CHAT_URL and sent["key"] == "k"
    assert sent["payload"]["model"] == "m/x"
    assert "Patching auth" in sent["payload"]["messages"][1]["content"]


def test_generate_plain_default_model_and_no_key(monkeypatch):
    from gateway import quick

    sent = {}
    monkeypatch.setattr(quick, "_openrouter", lambda url, payload, **kw: sent.update(payload) or {})
    monkeypatch.setattr(quick, "_settings", lambda: {})
    assert REAL_GENERATE({"summary": "x"}) is None and sent == {}
    monkeypatch.setattr(quick, "_settings", lambda: {"openrouter_key": "k"})
    assert REAL_GENERATE({"summary": "x"}) is None  # empty reply -> unusable
    assert sent["model"] == central.SUMMARY_MODEL_DEFAULT


# --- clear done ---------------------------------------------------------------

def test_clear_done_requires_ios_token():
    assert _call("POST", "/central/clear-done").status_code == 401
    assert _call("POST", "/central/clear-done", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


def test_clear_done_hides_done_and_persists(monkeypatch):
    monkeypatch.setattr(central, "_now", lambda: NOW)
    resp = _call("POST", "/central/clear-done", headers=IOS)
    assert resp.json() == {"cleared_before": "2026-10-15T12:00:00Z"}
    assert json.loads(central.STATE_PATH.read_text())["cleared_before"] == "2026-10-15T12:00:00Z"
    data = _call("GET", "/central/sessions", headers=IOS).json()
    assert [s["session_id"] for s in data] == ["s-run", "s-ask"]  # s-done (08:00) hidden


def test_reactivated_or_newer_done_session_reappears(monkeypatch):
    central._save_state({"cleared_before": "2026-10-15T12:00:00Z", "plain": {}})
    items = [
        {"session_id": "back", "state": "running", "updated_at": "2026-10-15T08:00:00Z"},
        {"session_id": "new-done", "state": "done", "updated_at": "2026-10-15T12:05:00Z"},
        {"session_id": "old-done", "state": "done", "updated_at": "2026-10-15T11:59:00Z"},
        {"session_id": "edge", "state": "done", "updated_at": "2026-10-15T12:00:00Z"},
        {"session_id": "no-ts", "state": "done"},
    ]
    data = _sessions(monkeypatch, items, now=datetime(2026, 10, 15, 12, 10, tzinfo=timezone.utc))
    assert [s["session_id"] for s in data] == ["back", "new-done"]


# --- floor idle filter --------------------------------------------------------

def test_floor_hidden_after_30_idle_minutes(monkeypatch):
    items = [
        {"session_id": "m29", "state": "running", "updated_at": "2026-10-15T11:31:00Z"},
        {"session_id": "m31", "state": "running", "updated_at": "2026-10-15T11:29:00Z"},
        {"session_id": "ask2h", "state": "needs_input", "updated_at": "2026-10-15T10:00:00Z"},
        {"session_id": "naive31", "state": "done", "updated_at": "2026-10-15T11:29:00"},
        {"session_id": "offset", "state": "running", "updated_at": "2026-10-15T14:45:00+03:00"},
        {"session_id": "junk", "state": "running", "updated_at": "yesterday"},
    ]
    data = {s["session_id"]: s["floor_hidden"] for s in _sessions(monkeypatch, items)}
    assert data == {
        "m29": False, "m31": True, "ask2h": False, "naive31": True, "offset": False, "junk": False,
    }


def test_floor_page_filters_hidden_sessions():
    html = central.FLOOR_HTML.read_text()
    assert "!x.floor_hidden" in html


# --- live status: last_activity_at + health -----------------------------------

def test_activity_and_health_pass_through_unchanged(monkeypatch):
    items = [
        {"session_id": "a", "state": "running", "updated_at": "2026-10-15T08:00:00Z",
         "last_activity_at": "2026-10-15T11:59:40Z", "health": "stalled"},
        {"session_id": "b", "state": "done", "health": "some_new_value",
         "last_activity_at": "2026-10-15T11:00:00.123Z"},
    ]
    data = _sessions(monkeypatch, items)
    assert (data[0]["last_activity_at"], data[0]["health"]) == ("2026-10-15T11:59:40Z", "stalled")
    assert (data[1]["last_activity_at"], data[1]["health"]) == ("2026-10-15T11:00:00.123Z", "some_new_value")


def test_activity_and_health_fallback_for_old_router(monkeypatch):
    items = [
        {"session_id": "run", "state": "running", "updated_at": "2026-10-15T11:00:00Z"},
        {"session_id": "ask", "state": "needs_input", "updated_at": "2026-10-15T10:00:00Z"},
        {"session_id": "done", "state": "done", "updated_at": "2026-10-15T12:00:00Z"},
        {"session_id": "bare", "health": None, "last_activity_at": ""},
    ]
    data = {s["session_id"]: (s["last_activity_at"], s["health"]) for s in _sessions(monkeypatch, items)}
    assert data == {
        "run": ("2026-10-15T11:00:00Z", "working"),
        "ask": ("2026-10-15T10:00:00Z", "waiting_for_owner"),
        "done": ("2026-10-15T12:00:00Z", None),
        "bare": (None, "working"),
    }


def test_floor_idle_uses_last_activity(monkeypatch):
    items = [
        # updated long ago, but real activity 5 min ago -> shown
        {"session_id": "busy", "state": "running", "updated_at": "2026-10-15T09:00:00Z",
         "last_activity_at": "2026-10-15T11:55:00Z"},
        # stalled 40 min and router says waiting for owner -> still shown
        {"session_id": "wait", "state": "running", "health": "waiting_for_owner",
         "last_activity_at": "2026-10-15T11:20:00Z"},
        {"session_id": "old", "state": "running", "last_activity_at": "2026-10-15T11:20:00Z"},
    ]
    data = {s["session_id"]: s["floor_hidden"] for s in _sessions(monkeypatch, items)}
    assert data == {"busy": False, "wait": False, "old": True}
