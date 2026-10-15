"""Sleep-PC: POST /pc/sleep (bearer, wake.py) + the cookie-authed twin
POST /screen/windows/sleep (screen.py) + the page's Sleep button markup.

The screen page's session cookie is Path=/screen, so its button can never
reach /pc/* — that's why the twin exists. Uses the shared gateway_server
fixture (conftest.py) with a fake role=windows WS client, same shape as
test_agents_command.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import websockets

from conftest import AUTH_HEADER, IOS_AUTH_HEADER, WS_HEADERS_KWARG


def test_pc_sleep_round_trip_to_fake_agent(gateway_server):
    """Agent online: the {cmd: sleep} envelope is forwarded, its reply maps to
    the /pc/wake-style {sent, ok, data} shape."""

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(
                f"ws://{base}/ws?role=windows", **{WS_HEADERS_KWARG: AUTH_HEADER}
            ) as agent_ws:
                async def answer_one() -> None:
                    envelope = json.loads(await asyncio.wait_for(agent_ws.recv(), timeout=10))
                    assert envelope["type"] == "command", envelope
                    assert envelope["payload"] == {"cmd": "sleep"}, envelope
                    await agent_ws.send(json.dumps({
                        "id": envelope["id"], "ok": True, "data": "sleep requested",
                    }))

                async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                    reply_task = asyncio.create_task(answer_one())
                    resp = await http.post("/pc/sleep", headers=AUTH_HEADER)
                    await reply_task
                    assert resp.status_code == 200, resp.text
                    assert resp.json() == {
                        "sent": True, "ok": True, "data": "sleep requested",
                    }, resp.text

    asyncio.run(run())


def test_pc_sleep_without_agent_is_503_json(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/pc/sleep", headers=AUTH_HEADER)
            assert resp.status_code == 503, resp.text
            assert resp.json() == {"sent": False, "error": "pc offline"}, resp.text

    asyncio.run(run())


def test_pc_sleep_requires_auth(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/pc/sleep")
            assert resp.status_code == 401, resp.text

    asyncio.run(run())


def test_screen_page_sleep_button_and_cookie_route(gateway_server):
    """Served page carries the bottom-bar Sleep button and inlined cursor.js
    sleep helpers; the minted session cookie reaches the twin route."""

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(
                f"ws://{base}/ws?role=windows", **{WS_HEADERS_KWARG: AUTH_HEADER}
            ) as agent_ws:
                async def answer_one() -> None:
                    envelope = json.loads(await asyncio.wait_for(agent_ws.recv(), timeout=10))
                    assert envelope["payload"] == {"cmd": "sleep"}, envelope
                    await agent_ws.send(json.dumps({
                        "id": envelope["id"], "ok": True, "data": "sleep requested",
                    }))

                async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                    resp = await http.get("/screen?target=windows", headers=IOS_AUTH_HEADER)
                    assert resp.status_code == 200, resp.text
                    html = resp.text
                    assert 'id="sleep-btn"' in html, "sleep button missing"
                    assert 'aria-label="Put the PC to sleep"' in html
                    assert "function sleepTap" in html  # cursor.js helpers inlined
                    assert "function sleepLabel" in html
                    assert "/screen/windows/sleep" in html  # button posts to the twin
                    cookie = resp.cookies.get("screen_session")
                    assert cookie, resp.headers
                    reply_task = asyncio.create_task(answer_one())
                    # Secure cookie — httpx won't jar it over http, send explicitly.
                    resp = await http.post(
                        "/screen/windows/sleep", headers={"Cookie": f"screen_session={cookie}"}
                    )
                    await reply_task
                    assert resp.status_code == 200, resp.text
                    assert resp.json() == {"ok": True}, resp.text

    asyncio.run(run())


def test_screen_page_pauses_frame_and_input_polling_while_sleeping(gateway_server):
    """Regression for the sleep-then-instant-wake bug: the frame loop and
    sendEvents must stop round-tripping to the windows agent the moment Sleep
    fires — a poll landing inside the suspend grace window is what was
    waking the PC straight back up. Guard must be set BEFORE the fetch, and
    only cleared once the status poll reports the agent back online."""

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen?target=windows", headers=IOS_AUTH_HEADER)
            html = resp.text
            assert "var pcSleeping = false;" in html
            # guarded before both round-trips that reach the agent over the websocket
            assert re.search(
                r'function pullFrame\(\) \{\s*if \(target === "windows" && pcSleeping\)', html
            ), "pullFrame must skip while pcSleeping"
            assert re.search(
                r'function sendEvents\(events\) \{\s*if \(target === "windows" && pcSleeping\)', html
            ), "sendEvents must skip while pcSleeping"
            # set before the sleep fetch fires, not after
            assert re.search(r"pcSleeping = true;\s*fetch\(\"/screen/windows/sleep\"", html)
            # only cleared by the status poll noticing the agent is back online
            assert 's.online) pcSleeping = false;' in html

    asyncio.run(run())
