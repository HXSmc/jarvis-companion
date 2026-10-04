"""C-HIGH-1 check: the WS endpoint accepts BINARY frames, not just text.

The iOS URLSessionWebSocketTask (and some other clients) emit BINARY frames;
the old `receive_text()` raised KeyError on them and the socket dropped
(close 1006). The endpoint now uses raw `receive()` and decodes either frame
kind. This test drives the exact regression: a binary `handoff_open` gets the
normal `{id, ok, data}` reply (socket alive), and a binary unknown-type
message still gets the generic ack afterwards — proving the connection was
never dropped. Mirrors tests/test_ws_flows_dispatch.py's setup.
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
IOS_TOKEN = "jarvis_ios_test_token_456"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
IOS_AUTH_HEADER = {"Authorization": f"Bearer {IOS_TOKEN}"}
CHECKOUT_URL = "https://booking.example.com/checkout?flight=RUH-9AM"
# websockets 14.0 renamed connect()'s extra_headers to additional_headers.
_WS_HEADERS_KWARG = (
    "additional_headers"
    if int(websockets.version.version.split(".")[0]) >= 14
    else "extra_headers"
)


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
    # Name-aware monkeypatch (S2): ios -> ios_token, else the shared token.
    config.get_token = lambda path=None, name="token": IOS_TOKEN if name == "ios_token" else TOKEN
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


def test_binary_frame_handoff_open_replies_and_keeps_socket_open():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/flight", headers=AUTH_HEADER, json={"url": CHECKOUT_URL, "context": "checkout"}
            )
            assert resp.status_code == 200, resp.text
            handoff_id = resp.json()["id"]

            async with websockets.connect(
                f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
            ) as app_ws:
                # BINARY frame: websockets sends a binary frame when handed bytes.
                await app_ws.send(json.dumps(
                    {"id": "b1", "type": "handoff_open", "payload": {"id": handoff_id}}
                ).encode("utf-8"))
                reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert reply == {"id": "b1", "ok": True, "data": {"url": CHECKOUT_URL}}, reply
                push = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert push == {"id": "b1", "type": "navigate", "payload": {"url": CHECKOUT_URL}}, push

                # Socket survived the binary frame: a follow-up (text) unknown
                # type still gets the generic ack on the same connection.
                await app_ws.send(json.dumps({"id": "b2", "type": "ping", "payload": {}}))
                ack = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert ack == {"id": "b2", "ok": True, "data": {}}, ack

            handoff = await flight.get_handoff(handoff_id)
            assert handoff["state"] == "driving", handoff

    asyncio.run(run())


def test_binary_garbage_gets_error_reply_socket_stays_open():
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(
                f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
            ) as app_ws:
                await app_ws.send(b"\xff\xfe not json at all")  # binary, non-UTF-8-ish garbage
                reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert reply == {"id": None, "ok": False, "data": {"error": "invalid envelope"}}, reply
                # Still open: a valid binary envelope round-trips afterwards.
                await app_ws.send(json.dumps({"id": "b3", "type": "ping", "payload": {"k": 1}}).encode())
                ack = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert ack == {"id": "b3", "ok": True, "data": {"k": 1}}, ack

    asyncio.run(run())
