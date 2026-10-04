"""T5.1 check: flight handoff flow — link format, pending->accepted->driving, TTL expiry.

Starts `gateway.app` in-process under uvicorn (as an asyncio task, port 0, same
event loop as the test) with `common.config.get_token` monkeypatched to a fixture
token — same approach as tests/test_ws_registry.py, so this passes regardless of
vault state. A fake app connects over WS as role=ios; the `handoff_open`
dispatch is driven through `gateway.flights.flight.handle_handoff_open` (the
importable WS-handling seam) so the fake app really receives the reply and the
navigate push through the live registry. The fake notify channel replaces
`flight.notify` for the TTL test.
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
from websockets.exceptions import ConnectionClosed, InvalidStatus

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:  # InvalidStatusCode is deprecated in websockets >=14 but raised pre-13
        from websockets.exceptions import InvalidStatusCode
    except ImportError:  # pragma: no cover
        InvalidStatusCode = InvalidStatus

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


def test_link_format_and_pending_state():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/flight",
                headers=AUTH_HEADER,
                json={"url": CHECKOUT_URL, "context": "9am Riyadh flight"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert set(body) == {"id", "link"}, body
            # Link format: jarvis://handoff/ prefix + non-empty id, and they match.
            assert body["link"].startswith("jarvis://handoff/"), body["link"]
            handoff_id = body["link"][len("jarvis://handoff/"):]
            assert handoff_id and handoff_id == body["id"], body
            handoff = await flight.get_handoff(handoff_id)
            assert handoff is not None and handoff["state"] == "pending", handoff
            assert handoff["url"] == CHECKOUT_URL and handoff["context"] == "9am Riyadh flight"

    asyncio.run(run())


def test_handoff_open_state_machine_and_navigate_push():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/flight",
                headers=AUTH_HEADER,
                json={"url": CHECKOUT_URL, "context": "checkout"},
            )
            handoff_id = resp.json()["id"]

            # Fake app connects over WS as role=ios (Bearer header, S2 protocol).
            async with websockets.connect(
                f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
            ) as app_ws:
                # The app's handoff_open {id, type, payload} message, dispatched to the
                # flow's importable WS-handling seam (gateway.ws routes envelopes; the
                # flight flow owns handoff_open).
                reply_data = await flight.handle_handoff_open("m1", handoff_id)
                assert reply_data == {"url": CHECKOUT_URL}, reply_data

                first = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert first == {"id": "m1", "ok": True, "data": {"url": CHECKOUT_URL}}, first
                second = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert second == {
                    "id": "m1",
                    "type": "navigate",
                    "payload": {"url": CHECKOUT_URL},
                }, second

            handoff = await flight.get_handoff(handoff_id)
            assert handoff["state"] == "driving", handoff
            assert handoff["history"] == ["pending", "accepted", "driving"], handoff["history"]

    asyncio.run(run())


def test_second_handoff_open_is_rejected_as_used():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/flight",
                headers=AUTH_HEADER,
                json={"url": CHECKOUT_URL, "context": "checkout"},
            )
            handoff_id = resp.json()["id"]

            async with websockets.connect(
                f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
            ) as app_ws:
                # First open: reply + navigate (MED-1 single-use happy path).
                reply_data = await flight.handle_handoff_open("m1", handoff_id)
                assert reply_data == {"url": CHECKOUT_URL}, reply_data
                for _ in range(2):  # drain reply + navigate push
                    await asyncio.wait_for(app_ws.recv(), timeout=10)

                # Second open of the SAME handoff: ok:False bare-string reply,
                # no second navigate push.
                reply_data = await flight.handle_handoff_open("m2", handoff_id)
                assert reply_data is None, reply_data
                reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert reply == {"id": "m2", "ok": False, "data": "handoff already used"}, reply

            handoff = await flight.get_handoff(handoff_id)
            assert handoff["state"] == "driving", handoff
            assert handoff["history"] == ["pending", "accepted", "driving"], handoff["history"]

    asyncio.run(run())


def test_ttl_expiry_notifies_once():
    async def run():
        recorded: list[dict] = []

        async def fake_notify(event: dict) -> None:
            recorded.append(event)

        flight.notify = fake_notify
        handoff = flight.create_handoff(CHECKOUT_URL, "ctx", ttl=0)  # test seam: expire at once
        await asyncio.sleep(0.01)

        first = await flight.get_handoff(handoff["id"])
        assert first["state"] == "expired", first
        second = await flight.get_handoff(handoff["id"])  # re-read must not re-notify
        assert second["state"] == "expired", second

        assert recorded == [{"id": handoff["id"], "event": "expired"}], recorded

    asyncio.run(run())
