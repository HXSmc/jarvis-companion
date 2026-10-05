"""`/siri/hey` accepts the phone's `ios_token` as well as the shared token.

Security-review fix 2026-10-05: the iPhone app sends `Authorization: Bearer
<ios_token>` to POST /siri/hey, so the shared-token-only dependency 401'd
every phone ask. The route now takes `require_bearer_any("token",
"ios_token")` — anything else still gets 401. Monkeypatches
`common.config.get_token` name-aware; the vault is never touched.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from common import config

TOKEN = "jarvis_test_token_123"
IOS_TOKEN = "jarvis_ios_test_token_456"


@pytest.fixture
def both_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config, "get_token",
        lambda path=None, name="token": TOKEN if name == "token" else IOS_TOKEN,
    )


def _post(bearer: str | None) -> httpx.Response:
    """POST {"text": "status"} with the given bearer (None = no header)."""
    from gateway.app import app

    async def run() -> httpx.Response:
        headers = {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/siri/hey", json={"text": "status"}, headers=headers)

    return asyncio.run(run())


def test_ios_token_gets_status_reply(both_tokens: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import siri

    monkeypatch.setattr(siri, "roles", lambda: ["ios"])

    resp = _post(IOS_TOKEN)

    assert resp.status_code == 200
    assert resp.json() == {"reply": "Jarvis gateway online. Connected: ios"}


def test_shared_token_still_works(both_tokens: None) -> None:
    resp = _post(TOKEN)
    assert resp.status_code == 200
    assert resp.json()["reply"].startswith("Jarvis gateway online.")


def test_wrong_bearer_is_401(both_tokens: None) -> None:
    assert _post("definitely-not-a-token").status_code == 401


def test_missing_header_is_401(both_tokens: None) -> None:
    assert _post(None).status_code == 401


def test_ios_token_unset_falls_back_to_shared_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `ios_token:` line in the vault → only the shared token opens the door."""
    monkeypatch.setattr(
        config, "get_token", lambda path=None, name="token": TOKEN if name == "token" else None
    )

    assert _post(TOKEN).status_code == 200
    assert _post(IOS_TOKEN).status_code == 401
