"""T6.1 check: gateway.ws dispatches {id, type, payload} envelopes by type.

Same approach as tests/test_flight_flow.py: the real app runs in-process under
uvicorn (port 0) with `common.config.get_token` monkeypatched to a fixture
token. A handoff is created via POST /handoff/flight (bearer auth), a fake ios
client connects to /ws?role=ios&token=... and sends a handoff_open envelope,
and the full WS round-trip is asserted: the {id, ok, data:{url}} reply and the
{type:"navigate"} push both arrive on the same socket, the handoff ends up
driving, and an unknown handoff id is replied with ok: False without dropping
the connection.
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
from gateway.flows import flight

TOKEN = "jarvis_test_token_123"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
CHECKOUT_URL = "https://booking.example.com/checkout?flight=RUH-9AM"


@pytest.fixture(autouse=True)
def clean_flight_state():
    flight._handoffs.clear()
    flight.notifications.clear()
    flight.notify = flight._default_notify
    yield
    flight._handoffs.clear()
    flight.notifications.clear()
    flight.notify = flight._default_notify


@contextlib.asynccontextmanager
async def gateway_server():
    """Run the app in-process on port 0 in the caller's event loop; yields host:port."""
    config.get_token = lambda path=None: TOKEN  # monkeypatch, per tests/test_ws_registry.py
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
        server.should_exit = True
        await serve_task


async def _create_handoff(http: httpx.AsyncClient) -> str:
    resp = await http.post(
        "/handoff/flight", headers=AUTH_HEADER, json={"url": CHECKOUT_URL, "context": "checkout"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_handoff_open_dispatch_replies_pushes_and_drives():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            handoff_id = await _create_handoff(http)

            async with websockets.connect(f"ws://{base}/ws?role=ios&token={TOKEN}") as app_ws:
                await app_ws.send(json.dumps(
                    {"id": "d1", "type": "handoff_open", "payload": {"id": handoff_id}}
                ))
                # (a) reply {id, ok: true, data: {url}} on the same socket.
                reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert reply == {"id": "d1", "ok": True, "data": {"url": CHECKOUT_URL}}, reply
                # (b) navigate push arrives on the same socket.
                push = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert push == {
                    "id": "d1",
                    "type": "navigate",
                    "payload": {"url": CHECKOUT_URL},
                }, push

            # (c) state advanced to driving.
            handoff = await flight.get_handoff(handoff_id)
            assert handoff["state"] == "driving", handoff

    asyncio.run(run())


def test_unknown_handoff_id_replies_error_socket_stays_open():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            async with websockets.connect(f"ws://{base}/ws?role=ios&token={TOKEN}") as app_ws:
                # (d) unknown handoff id -> ok: False, no dropped connection.
                await app_ws.send(json.dumps(
                    {"id": "d2", "type": "handoff_open", "payload": {"id": "no-such-handoff"}}
                ))
                reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert reply["id"] == "d2" and reply["ok"] is False, reply
                assert reply["data"], reply
                # Socket still open: an unknown type still gets the generic ack.
                await app_ws.send(json.dumps({"id": "d3", "type": "ping", "payload": {}}))
                ack = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert ack == {"id": "d3", "ok": True, "data": {}}, ack

    asyncio.run(run())
