"""Split deploy: with MAC_GATEWAY_URL set, /screen/mac/* is forwarded to the
Mac's own gateway (fake ASGI app here) with the ios_token; other targets and
the in-process path are covered by test_screen_endpoints.py."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request, Response

from conftest import AUTH_HEADER, IOS_AUTH_HEADER, IOS_TOKEN
from gateway import screen

FAKE_JPEG = b"mac-jpeg"


def _fake_mac(seen: list[tuple[str, str, str | None]]) -> FastAPI:
    mac = FastAPI()

    def record(request: Request) -> None:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))

    @mac.get("/screen/mac/status")
    async def status(request: Request):
        record(request)
        return {"target": "mac", "online": True, "tcc_ok": True, "display_asleep": False}

    @mac.get("/screen/mac/frame")
    async def frame(request: Request, w: int, q: int):
        record(request)
        if request.headers.get("if-none-match") == "abc":
            return Response(status_code=304, headers={"ETag": "abc"})
        return Response(content=FAKE_JPEG, media_type="image/jpeg", headers={"ETag": "abc"})

    @mac.post("/screen/mac/input")
    async def input_(request: Request):
        record(request)
        body = await request.json()
        assert body["events"][0]["type"] == "tap"
        return {"ok": True}

    @mac.post("/screen/mac/open")
    async def open_(request: Request):
        record(request)
        raise HTTPException(status_code=422, detail="app not found")

    return mac


@pytest.fixture
def mac_proxy(monkeypatch):
    seen: list[tuple[str, str, str | None]] = []
    transport = httpx.ASGITransport(app=_fake_mac(seen))
    monkeypatch.setenv(screen.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(screen, "_mac_client", lambda: httpx.AsyncClient(transport=transport))
    return seen


def test_mac_routes_forward_with_ios_token(gateway_server, mac_proxy):
    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                status = await c.get("/screen/mac/status")
                assert status.status_code == 200 and status.json()["online"] is True
                frame = await c.get("/screen/mac/frame?w=800&q=50")
                assert frame.status_code == 200 and frame.content == FAKE_JPEG
                assert frame.headers["etag"] == "abc"
                cached = await c.get("/screen/mac/frame", headers={"If-None-Match": "abc"})
                assert cached.status_code == 304
                tap = await c.post("/screen/mac/input", json={"events": [{"type": "tap", "x": 0.5, "y": 0.5}]})
                assert tap.status_code == 200 and tap.json() == {"ok": True}
                opened = await c.post("/screen/mac/open", json={"name": "Nope"})
                assert opened.status_code == 422 and opened.json()["detail"] == "app not found"
    asyncio.run(run())
    assert [path for _, path, _ in mac_proxy] == [
        "/screen/mac/status", "/screen/mac/frame", "/screen/mac/frame",
        "/screen/mac/input", "/screen/mac/open",
    ]
    assert {auth for _, _, auth in mac_proxy} == {f"Bearer {IOS_TOKEN}"}


def test_mac_proxy_still_needs_local_auth(gateway_server, mac_proxy):
    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}") as c:
                assert (await c.get("/screen/mac/status")).status_code == 401
                assert (await c.get("/screen/mac/status", headers=AUTH_HEADER)).status_code == 403
    asyncio.run(run())
    assert mac_proxy == []


def test_mac_unreachable_is_offline(gateway_server, monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setenv(screen.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(screen, "_mac_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)))

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                status = await c.get("/screen/mac/status")
                assert status.status_code == 200 and status.json()["online"] is False
                frame = await c.get("/screen/mac/frame")
                assert frame.status_code == 503 and frame.json()["detail"] == screen.MAC_OFFLINE_DETAIL
    asyncio.run(run())


@pytest.fixture
def mac_lock(monkeypatch):
    """Fake Mac gateway for focus-lock proxy tests; the test drives it via `state`."""
    state = {"focus_lock": True, "status": 200, "detail": None, "bodies": []}
    mac = FastAPI()

    @mac.get("/screen/mac/status")
    async def status():
        payload = {"target": "mac", "online": True, "tcc_ok": True, "display_asleep": False}
        if state["focus_lock"] != "absent":
            payload["focus_lock"] = state["focus_lock"]
        return payload

    @mac.post("/screen/mac/input")
    async def input_(request: Request):
        state["bodies"].append(await request.json())
        if state["status"] == 200:
            return {"ok": True}
        raise HTTPException(status_code=state["status"], detail=state["detail"])

    transport = httpx.ASGITransport(app=mac)
    monkeypatch.setenv(screen.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(screen, "_mac_client", lambda: httpx.AsyncClient(transport=transport))
    monkeypatch.setattr(screen, "_input_times", {})
    return state


def test_proxy_forwards_lock_fields(gateway_server, mac_lock):
    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                resp = await c.post(
                    "/screen/mac/input",
                    json={"events": [{"type": "text", "text": "hi",
                                      "require_app": "TextEdit", "require_window": "Doc"}]},
                )
                assert resp.status_code == 200, resp.text
        event = mac_lock["bodies"][0]["events"][0]
        assert event["require_app"] == "TextEdit", event
        assert event["require_window"] == "Doc", event

    asyncio.run(run())


def test_proxy_passes_mac_409_through(gateway_server, mac_lock):
    detail = {
        "error": "focus_mismatch", "reason": "point", "expected_app": "TextEdit",
        "actual_app": "Finder", "window_matched": None, "refused_index": 0,
        "events_run": 0, "partial": False, "message": "x",
    }
    mac_lock["status"] = 409
    mac_lock["detail"] = detail

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                tap = {"type": "tap", "x": 0.5, "y": 0.5}
                resp = await c.post("/screen/mac/input", json={"events": [tap]})
                assert resp.status_code == 409, resp.text
                assert resp.json()["detail"] == detail, resp.text
                mac_lock["status"] = 500
                resp = await c.post("/screen/mac/input", json={"events": [tap]})
                assert resp.status_code == 503, resp.text

    asyncio.run(run())


def test_proxy_refuses_lock_when_mac_is_old(gateway_server, mac_lock):
    mac_lock["focus_lock"] = "absent"

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                locked_tap = {"type": "tap", "x": 0.5, "y": 0.5, "require_app": "Safari"}
                resp = await c.post("/screen/mac/input", json={"events": [locked_tap]})
                assert resp.status_code == 409, resp.text
                assert resp.json()["detail"] == screen.FOCUS_LOCK_UNSUPPORTED, resp.text
                assert mac_lock["bodies"] == [], mac_lock["bodies"]
                mac_lock["focus_lock"] = None
                resp = await c.post("/screen/mac/input", json={"events": [dict(locked_tap)]})
                assert resp.status_code == 409, resp.text
                assert mac_lock["bodies"] == [], mac_lock["bodies"]

    asyncio.run(run())


def test_proxy_unlocked_batch_skips_status_check(gateway_server, mac_lock):
    mac_lock["focus_lock"] = "absent"

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                resp = await c.post("/screen/mac/input",
                                    json={"events": [{"type": "tap", "x": 0.5, "y": 0.5}]})
                assert resp.status_code == 200, resp.text
                assert len(mac_lock["bodies"]) == 1, mac_lock["bodies"]

    asyncio.run(run())
