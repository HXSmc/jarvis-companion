"""Stubbed quick-ask + wake dispatch checks for /siri/hey (no network, no brain).

W1 criteria: each quick prefix routes to `quick.ask` with the prefix stripped
and never consumes a brain slot; wake intent variants call `wake.wake_pc` and
map its result / no-MAC 400 to the spoken replies; status, screen and plain
brain transcripts behave exactly as before the dispatch was inserted.
`quick.ask` and `wake.wake_pc` are monkeypatched — the vault, OpenRouter,
sockets and `claude -p` are never touched.
"""

from __future__ import annotations

import asyncio
import collections

import httpx
import pytest
from fastapi import HTTPException

from common import config


@pytest.fixture
def stubbed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "jarvis_test_token_123"
    # Name-aware like conftest's gateway_server; ios_token unset here (None).
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": token if name == "token" else None)
    return token


@pytest.fixture
def fresh_brain_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level rate-limit window and semaphore between tests."""
    from gateway import siri

    monkeypatch.setattr(siri, "_brain_call_times", collections.deque())
    monkeypatch.setattr(siri, "_brain_semaphore", asyncio.Semaphore(2))


def _post(stubbed_token: str | None, text: str) -> httpx.Response:
    """POST {"text": text} to /siri/hey over the ASGI transport (no live server)."""
    from gateway.app import app

    async def run() -> httpx.Response:
        headers = {} if stubbed_token is None else {"Authorization": f"Bearer {stubbed_token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/siri/hey", json={"text": text}, headers=headers)

    return asyncio.run(run())


def _stub_quick_ask(monkeypatch: pytest.MonkeyPatch, answer: str = "the terse answer") -> list[str]:
    """quick.ask stub returning `answer`; returns the list of questions it received."""
    from gateway import quick

    questions: list[str] = []

    async def fake_ask(q: str, brain: str = "auto") -> quick.QuickAnswer:
        questions.append(q)
        return quick.QuickAnswer(answer, "vault", 5, None)

    monkeypatch.setattr(quick, "ask", fake_ask)
    return questions


def _stub_wake_pc(
    monkeypatch: pytest.MonkeyPatch,
    result: dict | None = None,
    exc: Exception | None = None,
) -> list[None]:
    """wake.wake_pc stub returning `result` or raising `exc`; returns call markers."""
    from gateway import wake

    calls: list[None] = []

    async def fake_wake_pc() -> dict:
        calls.append(None)
        if exc is not None:
            raise exc
        return result  # type: ignore[return-value]

    monkeypatch.setattr(wake, "wake_pc", fake_wake_pc)
    return calls


def test_quick_prefix_routes_each_variant(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    questions = _stub_quick_ask(monkeypatch, answer="42")

    assert _post(stubbed_token, "quick what is 6 times 7").json() == {"reply": "42"}
    assert _post(stubbed_token, "question who won the match").json() == {"reply": "42"}
    assert _post(stubbed_token, "q: how tall is the tower").json() == {"reply": "42"}
    assert _post(stubbed_token, "? when does the store open").json() == {"reply": "42"}
    # Matching is case-insensitive on the stripped transcript; the prefix and its
    # whitespace are stripped, the question's own casing is preserved.
    assert _post(stubbed_token, "  Quick What is 2+2?").json() == {"reply": "42"}

    assert questions == [
        "what is 6 times 7",
        "who won the match",
        "how tall is the tower",
        "when does the store open",
        "What is 2+2?",
    ]


def test_quick_prefix_does_not_consume_brain(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero brain slots allowed: a brain-path transcript would answer busy, quick must not."""
    from gateway import siri

    questions = _stub_quick_ask(monkeypatch, answer="from the vault")
    monkeypatch.setattr(siri, "_BRAIN_MAX_CALLS", 0)

    resp = _post(stubbed_token, "quick what is my IP")

    assert resp.status_code == 200
    assert resp.json() == {"reply": "from the vault"}
    assert questions == ["what is my IP"]


def test_wake_intent_variants(stubbed_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_wake_pc(
        monkeypatch,
        {"sent": True, "online": True, "waited_ms": 23000, "mac": "aa:bb:cc:dd:ee:ff"},
    )

    for transcript in ("wake up my pc", "turn on the pc", "start pc"):
        assert _post(stubbed_token, transcript).json() == {"reply": "PC online in 23 seconds"}

    assert len(calls) == 3


def test_wake_offline_and_no_mac_replies(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_wake_pc(
        monkeypatch,
        {"sent": True, "online": False, "waited_ms": 90012, "mac": "aa:bb:cc:dd:ee:ff"},
    )
    assert _post(stubbed_token, "wake my pc").json() == {
        "reply": "PC didn't come online after 90 seconds — check Wake-on-LAN is enabled"
    }

    _stub_wake_pc(
        monkeypatch,
        exc=HTTPException(status_code=400, detail="no PC MAC configured: set pc_mac in the vault"),
    )
    assert _post(stubbed_token, "wake the pc").json() == {"reply": "no PC MAC configured"}


def test_status_and_screen_still_win(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering boundary: status phrases and screen intents never reach quick.ask."""
    from gateway import screen, siri

    questions = _stub_quick_ask(monkeypatch)
    monkeypatch.setattr(siri, "roles", lambda: ["ios"])
    launches: list[str] = []

    async def fake_launch(target: str) -> None:
        launches.append(target)

    monkeypatch.setattr(screen, "launch_screen", fake_launch)

    assert _post(stubbed_token, "are you there").json() == {
        "reply": "Jarvis gateway online. Connected: ios"
    }
    assert _post(stubbed_token, "show my mac screen").json() == {"reply": "Opening your Mac screen"}

    assert questions == []
    assert launches == ["mac"]


def test_plain_text_still_brains(
    stubbed_token: str, monkeypatch: pytest.MonkeyPatch, fresh_brain_limits: None
) -> None:
    """An unprefixed transcript must not be intercepted by the quick prefix."""
    from gateway import quick
    from mac.voice import brain

    async def explode(q: str, brain_flag: str = "auto") -> quick.QuickAnswer:
        raise AssertionError("quick.ask must not run for plain transcripts")

    monkeypatch.setattr(quick, "ask", explode)
    monkeypatch.setattr(brain, "reply", lambda *args: "a brain answer")

    resp = _post(stubbed_token, "tell me a joke")

    assert resp.status_code == 200
    assert resp.json() == {"reply": "a brain answer"}
