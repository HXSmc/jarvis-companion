"""T5.1 check: flight handoff flow — link format, pending->accepted->driving, TTL expiry.

Starts `gateway.app` via the shared tests/conftest.py `gateway_server` fixture
(in-process uvicorn as an asyncio task in the test's own loop, fixture tokens),
so this passes regardless of vault state. A fake app connects over WS as
role=ios; the `handoff_open`
dispatch is driven through `gateway.flights.flight.handle_handoff_open` (the
importable WS-handling seam) so the fake app really receives the reply and the
navigate push through the live registry. The fake notify channel replaces
`flight.notify` for the TTL test.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import websockets

from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from gateway.flows import flight

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


def test_link_format_and_pending_state(gateway_server):
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


def test_handoff_open_state_machine_and_navigate_push(gateway_server):
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


def test_second_handoff_open_is_rejected_as_used(gateway_server):
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


def test_untouched_handoff_expires_via_sweeper_without_get_handoff(monkeypatch):
    """C-MED-2: an ignored link expires through the background sweeper alone.

    The handoff is read straight from the module store — get_handoff is never
    called (that path had its own expiry) — and the notify channel fires
    exactly once.
    """
    async def run():
        recorded: list[dict] = []

        async def fake_notify(event: dict) -> None:
            recorded.append(event)

        flight.notify = fake_notify
        monkeypatch.setattr(flight, "SWEEP_INTERVAL_S", 0.05)
        handoff = flight.create_handoff(CHECKOUT_URL, "ctx", ttl=0.15)

        deadline = time.monotonic() + 5
        while (
            flight._handoffs[handoff["id"]]["state"] != "expired"
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)

        stored = flight._handoffs[handoff["id"]]  # direct store read — no get_handoff
        assert stored["state"] == "expired", stored
        assert stored["history"][-1] == "expired", stored["history"]
        # Give any (wrong) second notify a chance to fire before asserting once-ness.
        await asyncio.sleep(0.2)
        assert recorded == [{"id": handoff["id"], "event": "expired"}], recorded

    asyncio.run(run())


def test_get_handoff_endpoint_returns_state_and_404_unknown(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/flight",
                headers=AUTH_HEADER,
                json={"url": CHECKOUT_URL, "context": "checkout"},
            )
            handoff_id = resp.json()["id"]

            # Poll endpoint: {id, state, kind} for a live handoff (C-MED-2).
            state = await http.get(f"/handoff/{handoff_id}", headers=AUTH_HEADER)
            assert state.status_code == 200, state.text
            assert state.json() == {"id": handoff_id, "state": "pending", "kind": "flight"}

            # Unknown id -> 404, not 500/200.
            missing = await http.get("/handoff/no-such-handoff", headers=AUTH_HEADER)
            assert missing.status_code == 404, missing.text
            assert "not found" in missing.json()["detail"]

    asyncio.run(run())
