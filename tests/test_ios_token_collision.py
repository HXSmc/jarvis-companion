"""ios_token equal to the shared token: phone-only routes fail closed.

If the vault's `ios_token:` repeats the shared `token:`, the Shortcut's token
would open every phone-only route. The gateway then answers 503 on
ios_token-only routes, keeps /siri/hey off the router, rejects the ios
WebSocket role, and warns loudly at startup. `config.get_token` is
monkeypatched; the vault is never touched.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from common import config

SAME = "jarvis_same_token_789"


@pytest.fixture
def shared_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": SAME)


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(
                method, path, headers={"Authorization": f"Bearer {SAME}"}, **kwargs
            )

    return asyncio.run(run())


def test_chat_refused_503(shared_tokens: None) -> None:
    resp = _request("POST", "/chat", json={"text": "hi", "thread_id": "app-12345678"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "ios_token must differ from the shared token"


def test_inbox_refused_503(shared_tokens: None) -> None:
    assert _request("GET", "/inbox").status_code == 503


def test_no_auth_still_401(shared_tokens: None) -> None:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/inbox")

    assert asyncio.run(run()).status_code == 401


def test_siri_never_reaches_router(shared_tokens: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import siri

    calls: list[str] = []

    async def spy(text: str, thread_id: str, msg_id: str) -> dict:
        calls.append(text)
        return {"reply": "routed"}

    monkeypatch.setattr(siri, "_router_reply", spy)
    resp = _request("POST", "/siri/hey", json={"text": "deploy the thing"})
    assert resp.status_code == 200
    assert resp.json() == {"reply": siri._SHARED_TOKEN_REPLY}
    assert calls == []


def test_ws_ios_role_rejected(shared_tokens: None) -> None:
    from gateway import ws

    assert ws._expected_token(ws.IOS_ROLE) is None
    assert ws._expected_token("windows") == SAME


def test_startup_warns_loudly(shared_tokens: None, capsys: pytest.CaptureFixture[str]) -> None:
    from gateway.app import warn_if_ios_token_shared

    assert warn_if_ios_token_shared() is True
    assert "ios_token must differ from the shared token" in capsys.readouterr().err


def test_distinct_tokens_no_warning_and_inbox_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    from gateway.app import warn_if_ios_token_shared

    monkeypatch.setattr(
        config, "get_token", lambda path=None, name="token": SAME if name == "ios_token" else "other"
    )
    monkeypatch.setenv("JARVIS_VENDOR_WATCH_HOME", str(tmp_path))
    assert warn_if_ios_token_shared() is False
    assert capsys.readouterr().err == ""
    assert _request("GET", "/inbox").status_code == 200
