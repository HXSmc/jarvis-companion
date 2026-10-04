"""C-MED-3 check: the agents command round-trip and brain's router-first flow.

`POST /agents/windows/command` forwards `router.request_envelope(cmd)` to the
windows agent's live WS connection and awaits the matching `{id, ok, data}`
reply (pending-futures map). `mac.voice.brain.reply` routes an allowlisted
transcript through that endpoint and formats the data; on gateway failure it
returns `router.OFFLINE_REPLY` instead of ever asking the claude bridge about
PC state. Uses the shared gateway_server fixture from conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import websockets

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)

from conftest import AUTH_HEADER, TOKEN, WS_HEADERS_KWARG
from gateway import agents
from mac.voice import brain, router


def test_command_round_trip_to_fake_windows_agent(gateway_server):
    """A live role=windows WS client answers the forwarded envelope; the POST
    returns its {ok, data} verbatim."""

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(
                f"ws://{base}/ws?role=windows", **{WS_HEADERS_KWARG: AUTH_HEADER}
            ) as agent_ws:
                async def answer_one() -> None:
                    envelope = json.loads(await asyncio.wait_for(agent_ws.recv(), timeout=10))
                    assert envelope["type"] == "command", envelope
                    assert envelope["payload"] == {"cmd": "specs"}, envelope
                    await agent_ws.send(json.dumps({
                        "id": envelope["id"], "ok": True, "data": {"ram": "16 GB"},
                    }))

                async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                    reply_task = asyncio.create_task(answer_one())
                    resp = await http.post(
                        "/agents/windows/command",
                        headers=AUTH_HEADER,
                        json={"cmd": "specs"},
                    )
                    await reply_task
                    assert resp.status_code == 200, resp.text
                    assert resp.json() == {"ok": True, "data": {"ram": "16 GB"}}, resp.text

    asyncio.run(run())


def test_command_without_windows_connected_is_503(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/agents/windows/command", headers=AUTH_HEADER, json={"cmd": "specs"}
            )
            assert resp.status_code == 503, resp.text
            assert resp.json()["detail"] == agents.NOT_CONNECTED_DETAIL, resp.text

    asyncio.run(run())


def test_command_timeout_when_agent_silent_is_504(monkeypatch, gateway_server):
    monkeypatch.setattr(agents, "REPLY_TIMEOUT_S", 0.3)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(
                f"ws://{base}/ws?role=windows", **{WS_HEADERS_KWARG: AUTH_HEADER}
            ) as agent_ws:  # connected but never replies
                async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                    resp = await http.post(
                        "/agents/windows/command", headers=AUTH_HEADER, json={"cmd": "specs"}
                    )
                    assert resp.status_code == 504, resp.text
                    assert resp.json()["detail"] == agents.TIMEOUT_DETAIL, resp.text

    asyncio.run(run())


def test_brain_reply_formats_gateway_data_and_never_calls_claude(monkeypatch):
    """Routed transcript -> gateway data -> router.format_reply; the S1 claude
    bridge must never fire (sentinel raises if it does)."""
    monkeypatch.setattr(brain, "_gateway_command", lambda cmd: {"ram": "16 GB"})

    def no_claude(*args, **kwargs):
        raise AssertionError("claude bridge must not run for a routed command")

    monkeypatch.setattr(brain, "_claude_bridge", no_claude)
    reply = brain.reply("what is the PC RAM")
    assert reply == router.format_reply("specs", {"ram": "16 GB"}), reply
    assert "16 GB" in reply, reply


def test_brain_gateway_failure_returns_offline_reply(monkeypatch):
    """ConnectionError from the gateway step -> router.OFFLINE_REPLY, still
    without touching the claude bridge (the PC's state is not claude's to guess)."""

    def unreachable(cmd):
        raise ConnectionError("gateway unreachable: connection refused")

    monkeypatch.setattr(brain, "_gateway_command", unreachable)

    def no_claude(*args, **kwargs):
        raise AssertionError("claude bridge must not run when the gateway is down")

    monkeypatch.setattr(brain, "_claude_bridge", no_claude)
    assert brain.reply("what is the PC RAM") == router.OFFLINE_REPLY


def test_brain_unrouted_transcript_falls_back_to_claude(monkeypatch):
    """Non-PC transcripts skip the gateway entirely and use the S1 sandboxed
    claude bridge."""

    def no_gateway(cmd):
        raise AssertionError("gateway command must not run for an unrouted transcript")

    monkeypatch.setattr(brain, "_gateway_command", no_gateway)
    monkeypatch.setattr(brain, "_claude_bridge", lambda transcript, timeout_s: "claude says hi")
    assert brain.reply("tell me a joke") == "claude says hi"
