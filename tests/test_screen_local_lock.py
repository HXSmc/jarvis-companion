"""jfollow11a (Q28): on the in-process path (no MAC_GATEWAY_URL) a batch with require_app only reaches a
driver that says it enforces the lock (FOCUS_LOCK = True). A driver without the flag would drop
require_app and post unlocked, so the gateway refuses with the proxy path's 409 body; status says so."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

from conftest import IOS_AUTH_HEADER  # noqa: E402
from gateway import screen  # noqa: E402
from mac.screen import driver as mac_driver  # noqa: E402  (the conftest stub off the Mac)

LOCKED_TAP = {"type": "tap", "x": 0.5, "y": 0.5, "require_app": "Terminal"}
TAP = {"type": "tap", "x": 0.5, "y": 0.5}


@pytest.fixture
def spy(monkeypatch):
    """Local driver path, fresh rate limit, a send_events spy."""
    monkeypatch.setattr(screen, "_input_times", {})
    monkeypatch.delenv(screen.MAC_GATEWAY_ENV, raising=False)
    calls: list[list[dict]] = []
    monkeypatch.setattr(mac_driver, "send_events", lambda events, *, locked=False: calls.append(events))
    return calls


@pytest.fixture
def no_flag(monkeypatch):
    monkeypatch.delattr(mac_driver, "FOCUS_LOCK", raising=False)


@pytest.fixture
def flag(monkeypatch):
    monkeypatch.setattr(mac_driver, "FOCUS_LOCK", True, raising=False)


def _post(gateway_server, event):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            return await http.post("/screen/mac/input", headers=IOS_AUTH_HEADER, json={"events": [event]})
    return asyncio.run(run())


def _status(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            return await http.get("/screen/mac/status", headers=IOS_AUTH_HEADER)
    return asyncio.run(run())


def test_locked_batch_without_flag_is_refused(gateway_server, spy, no_flag):
    resp = _post(gateway_server, LOCKED_TAP)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "focus_lock_unsupported"
    assert spy == []  # nothing was sent


def test_unlocked_batch_without_flag_posts(gateway_server, spy, no_flag):
    resp = _post(gateway_server, TAP)
    assert resp.status_code == 200, resp.text
    assert len(spy) == 1


def test_locked_batch_with_flag_posts(gateway_server, spy, flag):
    resp = _post(gateway_server, LOCKED_TAP)
    assert resp.status_code == 200, resp.text
    assert len(spy) == 1 and spy[0][0]["require_app"] == "Terminal"


def test_status_focus_lock_follows_the_flag(gateway_server, spy, monkeypatch):
    monkeypatch.delattr(mac_driver, "FOCUS_LOCK", raising=False)
    resp = _status(gateway_server)
    assert resp.status_code == 200 and resp.json()["focus_lock"] is False, resp.text
    monkeypatch.setattr(mac_driver, "FOCUS_LOCK", True, raising=False)
    resp = _status(gateway_server)
    assert resp.status_code == 200 and resp.json()["focus_lock"] is True, resp.text
    monkeypatch.setattr(mac_driver, "FOCUS_LOCK", 1, raising=False)  # only a real True counts
    assert _status(gateway_server).json()["focus_lock"] is False
