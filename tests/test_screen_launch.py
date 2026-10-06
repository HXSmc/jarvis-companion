"""SC7: POST /screen/launch + voice screen intents — nonce mint, navigate push, router.

Uses the shared `gateway_server` fixture (conftest.py) with fixture tokens.
SCREEN_PUBLIC_BASE pins the launch URL so no tailscale call happens in tests;
the siri intercept rides test 1's shared-token call (it fires before any brain
call, so no claude subprocess is involved).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from gateway import screen
from mac.voice import router

PUBLIC_BASE = "https://screen-test.<tld>:8790"


@pytest.fixture(autouse=True)
def _public_base(monkeypatch):
    monkeypatch.setenv("SCREEN_PUBLIC_BASE", PUBLIC_BASE)


def test_launch_with_shared_token(gateway_server):
    async def run():
        screen._BOOTSTRAPS.clear()
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/screen/launch", headers=AUTH_HEADER, json={"target": "mac"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # Shared token gets pushed-only — no url/nonce key to trade for a session.
            assert body == {"pushed": False}, body
            assert "set-cookie" not in resp.headers  # launch never mints a session
            assert len(screen._BOOTSTRAPS) == 1  # nonce still minted for the WS push
            # The siri voice path uses the same launch (intercept, no brain call).
            resp = await http.post(
                "/siri/hey", headers=AUTH_HEADER, json={"text": "show my mac screen"}
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"reply": "Opening your Mac screen"}, resp.text

    asyncio.run(run())


def test_launch_with_ios_token(gateway_server):
    async def run():
        screen._BOOTSTRAPS.clear()
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/screen/launch", headers=IOS_AUTH_HEADER, json={"target": "windows"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["pushed"] is False, body
            assert body["url"].startswith(f"{PUBLIC_BASE}/screen?boot="), body
            assert body["url"].endswith("&target=windows"), body
            assert "set-cookie" not in resp.headers

    asyncio.run(run())


def test_router_maps_screen_intents():
    assert router.route("show my mac screen") == "screen_mac"
    assert router.route("open my pc screen") == "screen_windows"
    assert router.route("Open my Windows screen") == "screen_windows"
    assert router.route("show the computer screen") == "screen_windows"
    # Existing intents untouched (screen check leads, no shadowing either way).
    assert router.route("what's my ram") == "specs"
    assert router.route("hello there") is None
