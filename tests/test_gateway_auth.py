"""Stubbed-token checks for /health bearer auth (httpx ASGI transport, no live server).

Monkeypatches `common.config.get_token` so these pass regardless of vault state.
`gateway.auth` reads the token via `config.get_token()` at request time, so the
monkeypatch takes effect without touching the gateway code. httpx's ASGITransport
is async-only, so each check drives an AsyncClient via asyncio.run.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from common import config


@pytest.fixture
def stubbed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "jarvis_test_token_123"
    monkeypatch.setattr(config, "get_token", lambda path=None: token)
    return token


def _get(stubbed_token: str | None) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        headers = {} if stubbed_token is None else {"Authorization": f"Bearer {stubbed_token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/health", headers=headers)

    return asyncio.run(run())


def test_health_without_token_is_401(stubbed_token: str) -> None:
    assert _get(None).status_code == 401


def test_health_with_token_is_200_ok(stubbed_token: str) -> None:
    resp = _get(stubbed_token)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_require_bearer_non_ascii_token_is_401_not_500(stubbed_token: str) -> None:
    # LOW-5: hmac.compare_digest(str, str) raised TypeError on non-ASCII (a
    # 500); the byte compare must yield a plain 401 instead. Direct dependency
    # call — httpx refuses to SEND non-ASCII header values client-side, but
    # uvicorn delivers them to the app as latin-1-decoded strings.
    from fastapi import HTTPException

    from gateway.auth import require_bearer

    async def run() -> HTTPException | None:
        try:
            await require_bearer(authorization="Bearer járvïs-tökén")
        except HTTPException as exc:
            return exc
        return None

    exc = asyncio.run(run())
    assert exc is not None
    assert exc.status_code == 401
