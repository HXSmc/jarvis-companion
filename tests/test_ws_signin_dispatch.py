"""T6.2 check: gateway.ws routes a real WS handoff_open to the sign-in flow by kind.

Same harness as tests/test_ws_flows_dispatch.py (real app in-process under
uvicorn, port 0, `common.config.get_token` monkeypatched) plus the fixture
vault from tests/test_signin_flow.py (tmp_path file, `signin.VAULT_PATH`
monkeypatched, fake credentials only). A sign-in handoff is created via POST
/handoff/signin (bearer), a fake ios client sends the handoff_open envelope
over the socket, and the full WS round-trip is asserted: the {id, ok, data}
reply, the navigate push, and the credential prefill evaluateJS push carrying
the fixture username — while caplog at DEBUG proves the fixture password never
reaches a log record. A flight handoff opened the same way must still get its
navigate push (no T6.1 regression).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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
from gateway.flows import flight, signin

TOKEN = "jarvis_test_token_123"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
SIGNIN_URL = "https://login.example.com/signin"
CHECKOUT_URL = "https://booking.example.com/checkout?flight=RUH-9AM"
VAULT_KEY = "Example Site"
FAKE_USERNAME = "user@example.com"
FAKE_PASSWORD = "not-a-real-password"

# Fixture vault: same markdown shape as the real Secrets vault (T0.1) —
# `## <Section>` headings with `key: value` lines. Fake credentials only.
VAULT_FIXTURE = f"""\
# Secrets

## Some Other Project
api_key: sk-not-a-real-key

## {VAULT_KEY}
username: {FAKE_USERNAME}
password: {FAKE_PASSWORD}

## Jarvis Companion
token: {TOKEN}
"""


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    vault_file = tmp_path / "Projects.md"
    vault_file.write_text(VAULT_FIXTURE, encoding="utf-8")
    monkeypatch.setattr(signin, "VAULT_PATH", vault_file)
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


def test_signin_handoff_open_reaches_signin_flow_over_real_ws(caplog):
    async def run():
        with caplog.at_level(logging.DEBUG):
            async with gateway_server() as base, httpx.AsyncClient(
                base_url=f"http://{base}"
            ) as http:
                resp = await http.post(
                    "/handoff/signin",
                    headers=AUTH_HEADER,
                    json={"url": SIGNIN_URL, "vault_key": VAULT_KEY},
                )
                assert resp.status_code == 200, resp.text
                handoff_id = resp.json()["id"]

                # Fake app connects over WS as role=ios and sends the same
                # envelope a real iPhone would — the T6.2 bug was that this
                # always landed in the flight flow.
                async with websockets.connect(
                    f"ws://{base}/ws?role=ios&token={TOKEN}"
                ) as app_ws:
                    await app_ws.send(json.dumps(
                        {"id": "s1", "type": "handoff_open", "payload": {"id": handoff_id}}
                    ))
                    # (a) reply {id, ok: true, data: {url}} on the same socket.
                    reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                    assert reply == {"id": "s1", "ok": True, "data": {"url": SIGNIN_URL}}, reply
                    # navigate push (flight's shared machine) then (b) the
                    # prefill JS push from the sign-in flow.
                    push = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                    assert push == {
                        "id": "s1",
                        "type": "navigate",
                        "payload": {"url": SIGNIN_URL},
                    }, push
                    js_push = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                    assert js_push["id"] == "s1", js_push
                    assert js_push["type"] == "evaluateJS", js_push
                    assert FAKE_USERNAME in js_push["payload"]["js"], js_push

                handoff = await flight.get_handoff(handoff_id)
                assert handoff["state"] == "driving", handoff

        # (c) the fixture password appears in no log record at DEBUG.
        assert FAKE_PASSWORD not in caplog.text, caplog.text

    asyncio.run(run())


def test_flight_handoff_open_still_navigates_no_regression():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            # (d) a flight handoff opened the same way keeps flight behavior.
            resp = await http.post(
                "/handoff/flight",
                headers=AUTH_HEADER,
                json={"url": CHECKOUT_URL, "context": "checkout"},
            )
            assert resp.status_code == 200, resp.text
            handoff_id = resp.json()["id"]

            async with websockets.connect(f"ws://{base}/ws?role=ios&token={TOKEN}") as app_ws:
                await app_ws.send(json.dumps(
                    {"id": "f1", "type": "handoff_open", "payload": {"id": handoff_id}}
                ))
                reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert reply == {"id": "f1", "ok": True, "data": {"url": CHECKOUT_URL}}, reply
                push = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert push == {
                    "id": "f1",
                    "type": "navigate",
                    "payload": {"url": CHECKOUT_URL},
                }, push
                # No prefill push for flight handoffs: the next frame is a
                # generic ack for an unknown type, not an evaluateJS.
                await app_ws.send(json.dumps({"id": "f2", "type": "ping", "payload": {}}))
                ack = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert ack == {"id": "f2", "ok": True, "data": {}}, ack

            handoff = await flight.get_handoff(handoff_id)
            assert handoff["state"] == "driving", handoff

    asyncio.run(run())
