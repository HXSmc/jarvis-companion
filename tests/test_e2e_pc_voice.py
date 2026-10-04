"""T5.3 e2e: PC specs by voice, "hey jarvis, what's the PC's RAM" -> spoken reply.

Fakes the edges, keeps the gateway real (PLAN T5.3 binding acceptance):
- STT is a fixture transcript STRING (the whisper binary is not exercised).
- TTS is a FakeSay capturing the final text.
- The Windows agent is a fake: FakeAsk for the speak() checks, and a real WS
  client connected through the live app for the wire-shape check, which pushes
  via gateway.registry.send_to_role (PLAN's "WS loop").
Token monkeypatched per tests/test_gateway_auth.py, so no vault dependency.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
import uvicorn
import websockets

from common import config
from mac.voice.router import OFFLINE_REPLY, request_envelope, route, speak

TOKEN = "jarvis_test_token_123"
SPECS_TRANSCRIPT = "hey jarvis, what's the PC's RAM"
UNRELATED_TRANSCRIPT = "hey jarvis, what's the weather tomorrow"
# Fixture agent-reply data dict: a specs dict with a RAM value in it.
SPEC_DATA = {"os": "Windows 11", "cpu": "Intel i7", "ram": "16 GB"}


class FakeAsk:
    """Async cmd -> data callable that records calls and returns/raises on cue."""

    def __init__(self, data: dict | None = SPEC_DATA, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._data = data
        self._error = error

    async def __call__(self, cmd: str) -> dict:
        self.calls.append(cmd)
        if self._error is not None:
            raise self._error
        return self._data


class FakeSay:
    """Async text -> None callable capturing what would be spoken."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, text: str) -> None:
        self.texts.append(text)


def test_route_specs_intent() -> None:
    assert route(SPECS_TRANSCRIPT) == "specs"
    assert route("jarvis, how much memory does the computer have") == "specs"
    assert route("what CPU is in the PC") == "specs"


def test_route_other_allowlisted_intents() -> None:
    assert route("how long has the PC been running") == "uptime"
    assert route("how much disk space is left") == "disk"
    assert route("what processes are running on the PC") == "processes"


def test_route_unrelated_is_none() -> None:
    assert route(UNRELATED_TRANSCRIPT) is None


def test_speak_unrelated_makes_no_calls() -> None:
    ask, say = FakeAsk(), FakeSay()
    asyncio.run(speak(UNRELATED_TRANSCRIPT, ask=ask, say=say))
    assert ask.calls == []
    assert say.texts == []


def test_e2e_specs_reply_contains_ram() -> None:
    ask, say = FakeAsk(), FakeSay()
    asyncio.run(speak(SPECS_TRANSCRIPT, ask=ask, say=say))
    assert ask.calls == ["specs"]
    assert len(say.texts) == 1
    assert "16 GB" in say.texts[0], f"RAM value missing from spoken text: {say.texts[0]!r}"


def test_speak_windows_off() -> None:
    ask, say = FakeAsk(error=ConnectionError("agent offline")), FakeSay()
    asyncio.run(speak(SPECS_TRANSCRIPT, ask=ask, say=say))
    assert say.texts == [OFFLINE_REPLY]


def test_request_envelope_shape_and_fresh_ids() -> None:
    env = request_envelope("specs")
    assert set(env) == {"id", "type", "payload"}
    assert env["type"] == "command"
    assert env["payload"] == {"cmd": "specs"}
    assert isinstance(env["id"], str) and env["id"]
    assert env["id"] != request_envelope("specs")["id"]


async def _wire_run() -> None:
    from gateway import registry
    from gateway.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]

    # Fake windows agent: a real WS client through the real app.
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?role=windows&token={TOKEN}") as agent:
        for _ in range(100):
            if "windows" in registry.roles():
                break
            await asyncio.sleep(0.05)
        assert "windows" in registry.roles(), "agent never registered"

        envelope = request_envelope("specs")
        assert await registry.send_to_role("windows", envelope) is True
        received = json.loads(await asyncio.wait_for(agent.recv(), timeout=10))
        assert set(received) == {"id", "type", "payload"}, f"wire shape: {received}"
        assert received["type"] == "command"
        assert received["payload"] == {"cmd": "specs"}
        assert received == envelope, f"agent did not receive exactly the envelope: {received}"

    for _ in range(100):  # endpoint's finally-unregister must land before we exit
        if "windows" not in registry.roles():
            break
        await asyncio.sleep(0.05)
    assert "windows" not in registry.roles(), "stale windows registration leaked"
    server.should_exit = True


def test_wire_envelope_reaches_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "get_token", lambda path=None: TOKEN)
    asyncio.run(_wire_run())
