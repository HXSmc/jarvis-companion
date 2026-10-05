"""Stubbed-token checks for /siri/hey (httpx ASGI transport, no live server).

T4.2 fix criteria: bearer 401; `status` answered from the registry in under 2 s
(no brain call); a hung brain call times out into the canned reply. Monkeypatches
`common.config.get_token` and `mac.voice.brain.reply` so the vault and
`claude -p` are never touched.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from common import config


@pytest.fixture
def stubbed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "jarvis_test_token_123"
    # Name-aware like conftest's gateway_server; ios_token unset here (None).
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": token if name == "token" else None)
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
    from gateway import siri

    monkeypatch.setattr(siri, "roles", lambda: ["ios", "windows"])

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
    from gateway import siri

    monkeypatch.setattr(siri, "roles", lambda: [])

    resp, _ = _post(stubbed_token, "ping")

    assert resp.status_code == 200
    assert resp.json() == {"reply": "Jarvis gateway online. Connected: none"}


def test_hung_brain_times_out(stubbed_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import siri
    from mac.voice import brain

    release = threading.Event()
    # Blocks the worker thread like a hung `claude -p`; released right after the
    # response so pytest exit doesn't join a 30 s sleeper. siri passes the
    # subprocess timeout as a second arg (brain.reply(text, timeout_s)).
    monkeypatch.setattr(brain, "reply", lambda *args: release.wait(30))
    monkeypatch.setattr(siri, "BRAIN_TIMEOUT_SECONDS", 1)

    resp, elapsed = _post(stubbed_token, "tell me a joke")
    release.set()

    assert resp.status_code == 200
    assert resp.json() == {"reply": "Jarvis is taking too long, try again."}
    assert elapsed < 16.0


@pytest.fixture
def fresh_brain_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level rate-limit window and semaphore between tests."""
    import collections

    from gateway import siri

    monkeypatch.setattr(siri, "_brain_call_times", collections.deque())
    monkeypatch.setattr(siri, "_brain_semaphore", asyncio.Semaphore(2))


def test_third_concurrent_call_gets_busy(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch, fresh_brain_limits: None
) -> None:
    """Semaphore(2) full → immediate busy reply, no queueing."""
    from gateway.app import app
    from mac.voice import brain

    release = threading.Event()
    monkeypatch.setattr(brain, "reply", lambda *args: release.wait(30))

    async def run() -> list[httpx.Response]:
        headers = {"Authorization": f"Bearer {stubbed_token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = asyncio.create_task(client.post("/siri/hey", json={"text": "a"}, headers=headers))
            r2 = asyncio.create_task(client.post("/siri/hey", json={"text": "b"}, headers=headers))
            await asyncio.sleep(0.2)  # let both land in the executor
            third = await client.post("/siri/hey", json={"text": "c"}, headers=headers)
            done = await asyncio.gather(r1, r2)
            release.set()
            return [third, *done]

    third, *done = asyncio.run(run())
    assert third.status_code == 200
    assert third.json() == {"reply": "Jarvis is busy, try again in a moment."}


def test_rate_limit_at_20_per_60s(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch, fresh_brain_limits: None
) -> None:
    """21st brain call inside the 60 s window → busy reply."""
    from gateway import siri
    from mac.voice import brain

    monkeypatch.setattr(brain, "reply", lambda *args: "sure")

    async def run() -> httpx.Response:
        from gateway.app import app

        headers = {"Authorization": f"Bearer {stubbed_token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(siri._BRAIN_MAX_CALLS):
                resp = await client.post("/siri/hey", json={"text": "hi"}, headers=headers)
                assert resp.json() == {"reply": "sure"}
            return await client.post("/siri/hey", json={"text": "hi"}, headers=headers)

    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert resp.json() == {"reply": "Jarvis is busy, try again in a moment."}
