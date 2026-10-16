"""Split deploy: /inbox/*, box mode (MAC_GATEWAY_URL set) forwards to the Mac.

The box's own vendor-watch store is always empty (J1's watcher only runs on
the Mac, the only machine with example MCP access), so GET /inbox and the
mark-read route must forward to the Mac gateway's own copies of those routes
instead of reading the local store — same env + helper shape as
gateway/central.py's /central/agents proxy. POST /inbox/notify stays local
(it wakes whichever "ios" socket is connected to THIS gateway) but must skip
the guaranteed-path enqueue in box mode (the Mac's own local notify call
already ran it) and pull the real unread count from the Mac.

Also covers the relaxed loopback guard: a tailnet (<cgnat-ip>/10) caller is
let through (this is what lets the Mac's watcher reach the box cross-host),
a public address is still rejected.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from common import config
from gateway import inbox

TOKEN = "jarvis_test_token_123"
IOS_TOKEN = "jarvis_ios_test_token_456"
IOS = {"Authorization": f"Bearer {IOS_TOKEN}"}

MAC_INBOX_BODY = {
    "items": [{"id": "m1", "subject": "Deployment failed", "read": False}],
    "unread": 3,
}


@pytest.fixture(autouse=True)
def stub(monkeypatch: pytest.MonkeyPatch):
    tokens = {"token": TOKEN, "ios_token": IOS_TOKEN}
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": tokens.get(name))
    monkeypatch.setenv("JARVIS_VENDOR_WATCH_HOME", "/tmp/jarvis-split-deploy-test-unused")


def _fake_mac_app(seen: list[dict], inbox_body=None, inbox_status=200, read_status=200):
    mac = FastAPI()

    @mac.get("/inbox")
    async def get_inbox_(request: Request):
        seen.append(dict(request.headers))
        return JSONResponse(inbox_body if inbox_body is not None else MAC_INBOX_BODY, status_code=inbox_status)

    @mac.post("/inbox/{item_id}/read")
    async def mark_read_(item_id: str, request: Request):
        seen.append(dict(request.headers))
        if read_status == 404:
            return JSONResponse({"detail": "unknown inbox item"}, status_code=404)
        return JSONResponse({"ok": True, "unread": 2}, status_code=read_status)

    return mac


def _get(path: str) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.get(path, headers=IOS)

    return asyncio.run(run())


def _post(path: str) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.post(path, headers=IOS)

    return asyncio.run(run())


def test_get_inbox_forwards_to_mac_when_split_deploy(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setenv(inbox.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_mac_app(seen)), timeout=inbox.MAC_PROXY_TIMEOUT),
    )
    resp = _get("/inbox")
    assert resp.status_code == 200
    assert resp.json() == MAC_INBOX_BODY
    assert seen[0]["authorization"] == f"Bearer {IOS_TOKEN}"


def test_get_inbox_mac_down_is_empty_not_500(monkeypatch):
    monkeypatch.setenv(inbox.MAC_GATEWAY_ENV, "http://mac.test")

    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=inbox.MAC_PROXY_TIMEOUT),
    )
    resp = _get("/inbox")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "unread": 0}


def test_mark_read_forwards_to_mac(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setenv(inbox.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_mac_app(seen)), timeout=inbox.MAC_PROXY_TIMEOUT),
    )
    resp = _post("/inbox/m1/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "unread": 2}
    assert seen[0]["authorization"] == f"Bearer {IOS_TOKEN}"


def test_mark_read_unknown_id_404s_through_mac(monkeypatch):
    monkeypatch.setenv(inbox.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_fake_mac_app([], read_status=404)), timeout=inbox.MAC_PROXY_TIMEOUT
        ),
    )
    resp = _post("/inbox/no-such-id/read")
    assert resp.status_code == 404


def test_mark_read_mac_down_is_503(monkeypatch):
    monkeypatch.setenv(inbox.MAC_GATEWAY_ENV, "http://mac.test")

    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=inbox.MAC_PROXY_TIMEOUT),
    )
    resp = _post("/inbox/m1/read")
    assert resp.status_code == 503


def test_notify_box_mode_pulls_unread_from_mac_and_skips_guaranteed_path(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setenv(inbox.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_mac_app(seen)), timeout=inbox.MAC_PROXY_TIMEOUT),
    )

    def must_not_be_called(*a, **kw):
        raise AssertionError("guaranteed-path enqueue must not run in box mode")

    monkeypatch.setattr(inbox, "_enqueue_guaranteed", must_not_be_called)

    captured: dict = {}

    async def fake_send(role: str, envelope: dict) -> bool:
        captured["role"] = role
        captured["envelope"] = envelope
        return True

    monkeypatch.setattr(inbox.registry, "send_to_role", fake_send)

    resp = _post("/inbox/notify")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "delivered": True}
    assert captured["role"] == "ios"
    assert captured["envelope"]["type"] == "inbox_new"
    assert captured["envelope"]["payload"]["unread"] == MAC_INBOX_BODY["unread"]


def test_loopback_guard_accepts_tailnet_peer() -> None:
    """A tailnet CGNAT address (e.g. the Mac calling the box) gets through."""
    request = StarletteRequest({"type": "http", "client": ("<cgnat-ip>", 51515), "method": "POST"})
    inbox._loopback_guard(request)  # must not raise


def test_loopback_guard_still_rejects_loopback_literal() -> None:
    request = StarletteRequest({"type": "http", "client": ("127.0.0.1", 51515), "method": "POST"})
    inbox._loopback_guard(request)  # must not raise


def test_loopback_guard_rejects_public_address() -> None:
    for host in ("8.8.8.8", "<lan>.1.5", "::1", "10.0.0.5"):
        request = StarletteRequest({"type": "http", "client": (host, 51515), "method": "POST"})
        with pytest.raises(HTTPException) as exc:
            inbox._loopback_guard(request)
        assert exc.value.status_code == 403, host
