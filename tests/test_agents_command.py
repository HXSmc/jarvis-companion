"""C-MED-3 check: the agents command round-trip and brain's router-first flow.

`POST /agents/windows/command` forwards `router.request_envelope(cmd)` to the
windows agent's live WS connection and awaits the matching `{id, ok, data}`
reply (pending-futures map). `mac.voice.brain.reply` routes an allowlisted
transcript through that endpoint and formats the data; on gateway failure it
returns `router.OFFLINE_REPLY` instead of ever asking the claude bridge about
PC state. Real app in-process on port 0, same approach as test_ws_registry.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import uvicorn
import websockets

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)

from common import config
from gateway import agents
from mac.voice import brain, router

TOKEN = "jarvis_test_token_123"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
# websockets 14.0 renamed connect()'s extra_headers to additional_headers.
_WS_HEADERS_KWARG = (
    "additional_headers"
    if int(websockets.version.version.split(".")[0]) >= 14
    else "extra_headers"
)


@contextlib.asynccontextmanager
async def gateway_server():
    """Run the app in-process on port 0 in the caller's event loop; yields host:port."""
    # Name-aware monkeypatch (S2): ios -> ios_token, else the shared token.
    # Restored on exit — this file sorts before test_config.py, which reads the
    # real get_token, so a leaked stub breaks it.
    original_get_token = config.get_token
    config.get_token = lambda path=None, name="token": (
        "jarvis_ios_test_token_456" if name == "ios_token" else TOKEN
    )
    from gateway.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"127.0.0.1:{port}"
    finally:
        config.get_token = original_get_token
        server.should_exit = True
        await serve_task


def test_command_round_trip_to_fake_windows_agent():
    """A live role=windows WS client answers the forwarded envelope; the POST
    returns its {ok, data} verbatim."""

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(
                f"ws://{base}/ws?role=windows", **{_WS_HEADERS_KWARG: AUTH_HEADER}
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


def test_command_without_windows_connected_is_503():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/agents/windows/command", headers=AUTH_HEADER, json={"cmd": "specs"}
            )
            assert resp.status_code == 503, resp.text
            assert resp.json()["detail"] == agents.NOT_CONNECTED_DETAIL, resp.text

    asyncio.run(run())


def test_command_timeout_when_agent_silent_is_504(monkeypatch):
    monkeypatch.setattr(agents, "REPLY_TIMEOUT_S", 0.3)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(
                f"ws://{base}/ws?role=windows", **{_WS_HEADERS_KWARG: AUTH_HEADER}
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
