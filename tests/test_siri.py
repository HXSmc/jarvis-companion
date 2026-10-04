"""Stubbed-token checks for /siri/hey (httpx ASGI transport, no live server).

T4.2 fix criteria: bearer 401; `status` answered from the registry in under 2 s
(no brain call); a hung brain call times out into the canned reply. Monkeypatches
`common.config.get_token` and `mac.voice.brain.reply` so the vault and
`claude -p` are never touched.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types

import httpx
import pytest

from common import config


@pytest.fixture
def stubbed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "jarvis_test_token_123"
    monkeypatch.setattr(config, "get_token", lambda path=None: token)
    return token


def _post(stubbed_token: str | None, text: str) -> tuple[httpx.Response, float]:
    """POST and return (response, wire latency) — latency timed around the request
    only, since asyncio.run's teardown may still join an abandoned brain thread."""
    from gateway.app import app

    async def run() -> tuple[httpx.Response, float]:
        headers = {} if stubbed_token is None else {"Authorization": f"Bearer {stubbed_token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            start = time.monotonic()
            resp = await client.post("/siri/hey", json={"text": text}, headers=headers)
            return resp, time.monotonic() - start

    return asyncio.run(run())


def test_siri_without_token_is_401(stubbed_token: str) -> None:
    resp, _ = _post(None, "status")
    assert resp.status_code == 401


def test_status_reply_built_from_registry(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = types.ModuleType("gateway.registry")
    fake.roles = lambda: ["ios", "windows"]
    monkeypatch.setitem(sys.modules, "gateway.registry", fake)

    resp, elapsed = _post(stubbed_token, "status")

    assert resp.status_code == 200
    assert resp.json() == {"reply": "Jarvis gateway online. Connected: ios, windows"}
    assert elapsed < 2.0
    # Matching is lowercased+stripped, so any casing/whitespace hits the same path.
    assert _post(stubbed_token, "  STATUS ")[0].json() == {
        "reply": "Jarvis gateway online. Connected: ios, windows"
    }


def test_status_without_registry_says_none(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, "gateway.registry", raising=False)

    resp, _ = _post(stubbed_token, "ping")

    assert resp.status_code == 200
    assert resp.json() == {"reply": "Jarvis gateway online. Connected: none"}


def test_hung_brain_times_out(stubbed_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import siri
    from mac.voice import brain

    release = threading.Event()
    # Blocks the worker thread like a hung `claude -p`; released right after the
    # response so pytest exit doesn't join a 30 s sleeper.
    monkeypatch.setattr(brain, "reply", lambda transcript: release.wait(30))
    monkeypatch.setattr(siri, "BRAIN_TIMEOUT_SECONDS", 1)

    resp, elapsed = _post(stubbed_token, "tell me a joke")
    release.set()

    assert resp.status_code == 200
    assert resp.json() == {"reply": "Jarvis is taking too long, try again."}
    assert elapsed < 16.0
