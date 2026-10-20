"""S1: Mini PC VNC proxy — vnc_des, minipc screen routes, noVNC asset proxy, ws auth translation.

Uses the shared `gateway_server` fixture (conftest.py) with fixture tokens —
never the real vault. The websocket tests use the Starlette TestClient per the
S1 spec; the fixture is still requested for its name-aware `config.get_token`
patch so no test can read the vault.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import websockets
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import AUTH_HEADER, IOS_AUTH_HEADER

from gateway import vnc

MINIPC_VNC_DETAIL = "minipc target uses the VNC websocket"


@pytest.fixture(autouse=True)
def _reset_live():
    """TestClient tears handlers down asynchronously between tests — the L2 cap
    counter can lag; reset it so cap tests start from a known 0."""
    vnc._live = 0
    yield
    # Poll up to 2s for handler cleanup to complete
    start = time.monotonic()
    while vnc._live != 0 and time.monotonic() - start < 2:
        time.sleep(0.05)
    assert vnc._live == 0, "VNC session slot leaked"


# --- (a) vnc_des ---------------------------------------------------------------

def test_vnc_des_16_bytes_deterministic_and_password_sensitive():
    challenge = bytes(range(16))
    assert len(vnc.vnc_des("testpass", challenge)) == 16
    assert vnc.vnc_des("testpass", challenge) == vnc.vnc_des("testpass", challenge)
    assert vnc.vnc_des("testpass", challenge) != vnc.vnc_des("otherpass", challenge)


# --- (b) minipc screen routes --------------------------------------------------

def test_minipc_frame_input_open_404_status_shape(gateway_server, monkeypatch):
    async def fake_online():
        return False

    monkeypatch.setattr(vnc, "online", fake_online)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            headers = IOS_AUTH_HEADER
            resp = await http.get("/screen/minipc/frame", headers=headers)
            assert resp.status_code == 404, resp.text
            assert resp.json() == {"detail": MINIPC_VNC_DETAIL}, resp.text
            resp = await http.post(
                "/screen/minipc/input", headers=headers,
                json={"events": [{"type": "tap", "x": 0.5, "y": 0.5}]},
            )
            assert resp.status_code == 404, resp.text
            assert resp.json() == {"detail": MINIPC_VNC_DETAIL}, resp.text
            resp = await http.post("/screen/minipc/open", headers=headers, json={"name": "Safari"})
            assert resp.status_code == 404, resp.text
            assert resp.json() == {"detail": MINIPC_VNC_DETAIL}, resp.text
            mac = await http.get("/screen/mac/status", headers=headers)
            mini = await http.get("/screen/minipc/status", headers=headers)
            assert mini.status_code == 200, mini.text
            assert sorted(mini.json()) == sorted(mac.json()), mini.text
            assert mini.json()["online"] is False

    asyncio.run(run())


# --- (c) noVNC asset proxy ------------------------------------------------------

def test_novnc_manifest_pinned(gateway_server):
    """The shipped manifest is real: core/rfb.js present with a 64-hex sha256."""
    assert "core/rfb.js" in vnc._sha256_manifest
    assert len(vnc._sha256_manifest) >= 2
    for path, digest in vnc._sha256_manifest.items():
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), path


def test_novnc_proxy_allowlist_and_auth(gateway_server, monkeypatch):
    body = b"export default 1;"
    manifest = {
        "core/rfb.js": hashlib.sha256(body).hexdigest(),
        "vendor/pako/pako.esm.js": hashlib.sha256(body).hexdigest(),
    }
    monkeypatch.setattr(vnc, "_sha256_manifest", manifest)
    monkeypatch.setattr(vnc, "MINIPC_NOVNC_BASE", "https://novnc.test")  # env-driven now

    async def fake_fetch(path: str) -> tuple[int, bytes]:
        return 200, body

    monkeypatch.setattr(vnc, "_fetch", fake_fetch)
    vnc._cache.clear()

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen/minipc/novnc/core/rfb.js", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"].startswith("text/javascript")
            assert resp.content == body
            # Manifest allowlist: exact pinned paths only, traversal included.
            for path in ("../x.js", "core/x.css", "app/ui.js", "core/rfb.js.map"):
                resp = await http.get(
                    f"/screen/minipc/novnc/{path}", headers=IOS_AUTH_HEADER
                )
                assert resp.status_code == 404, (path, resp.status_code, resp.text)
            resp = await http.get("/screen/minipc/novnc/core/rfb.js")
            assert resp.status_code == 401, resp.text
            resp = await http.get("/screen/minipc/novnc/core/rfb.js", headers=AUTH_HEADER)
            assert resp.status_code == 403, resp.text

    asyncio.run(run())


def test_novnc_hash_mismatch_502_not_cached(gateway_server, monkeypatch):
    monkeypatch.setattr(vnc, "_sha256_manifest", {"core/rfb.js": "0" * 64})
    monkeypatch.setattr(vnc, "MINIPC_NOVNC_BASE", "https://novnc.test")  # env-driven now

    async def fake_fetch(path: str) -> tuple[int, bytes]:
        return 200, b"tampered bytes"

    monkeypatch.setattr(vnc, "_fetch", fake_fetch)
    vnc._cache.clear()

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen/minipc/novnc/core/rfb.js", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 502, resp.text
            assert "core/rfb.js" not in vnc._cache  # tampered bytes are never cached

    asyncio.run(run())


def test_novnc_upstream_non200_and_error(gateway_server, monkeypatch):
    monkeypatch.setattr(vnc, "_sha256_manifest", {"vendor/ui.js": "0" * 64})
    monkeypatch.setattr(vnc, "MINIPC_NOVNC_BASE", "https://novnc.test")  # env-driven now
    vnc._cache.clear()

    async def not_found(path: str) -> tuple[int, bytes]:
        return 404, b""

    async def boom(path: str) -> tuple[int, bytes]:
        raise RuntimeError("upstream down")

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            monkeypatch.setattr(vnc, "_fetch", boom)
            resp = await http.get(
                "/screen/minipc/novnc/vendor/ui.js", headers=IOS_AUTH_HEADER
            )
            assert resp.status_code == 502, resp.text
            monkeypatch.setattr(vnc, "_fetch", not_found)
            resp = await http.get(
                "/screen/minipc/novnc/vendor/ui.js", headers=IOS_AUTH_HEADER
            )
            assert resp.status_code == 404, resp.text

    asyncio.run(run())


# --- (d) websocket auth translation ---------------------------------------------

class _FakeRFB:
    """Scripted upstream websockify: offers {None, VNC}, expects the DES response."""

    def __init__(self, password: str):
        self.password = password
        self.port = 0
        self.closed = threading.Event()  # set when the gateway closes the upstream
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5), "fake RFB server did not start"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def handler(ws):
            try:
                challenge = bytes(range(16))
                await ws.send(b"RFB 003.008\n")
                await ws.recv()
                await ws.send(b"\x01\x02")
                await ws.recv()
                await ws.send(challenge)
                response = await ws.recv()
                if response == vnc.vnc_des(self.password, challenge):
                    await ws.send(b"\x00\x00\x00\x00")
                    await ws.send(b"SRVINIT")
                    async for msg in ws:
                        await ws.send(msg)
                        return  # ponytail: echo once then close — the gateway must
                        # end the client session before TestClient's exit-cancel
                        # runs, or the session-exit future raises CancelledError.
                else:
                    reason = b"auth failed"
                    await ws.send(b"\x00\x00\x00\x01" + len(reason).to_bytes(4, "big") + reason)
            finally:
                self.closed.set()

        async def start():
            self._server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]

        loop.run_until_complete(start())
        self._ready.set()
        loop.run_forever()


def _client() -> TestClient:
    from gateway.app import app

    return TestClient(app)


# TestClient.websocket_connect mutates the headers dict it's given
# (setdefault of sec-websocket-key etc.) — always pass a copy, or conftest's
# shared IOS_AUTH_HEADER gets polluted and later tests send duplicate
# Sec-WebSocket-Key headers (server 400s them).
_WS_HEADERS = dict(IOS_AUTH_HEADER)


def test_ws_happy_path(gateway_server, monkeypatch):
    fake = _FakeRFB("testpass")
    monkeypatch.setattr(vnc, "MINIPC_VNC_WS", f"ws://127.0.0.1:{fake.port}")
    monkeypatch.setattr(vnc, "vnc_password", lambda: "testpass")
    with _client().websocket_connect(
        "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
    ) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"\x01\x01"
        ws.send_bytes(b"\x01")
        assert ws.receive_bytes() == b"\x00\x00\x00\x00"
        assert ws.receive_bytes() == b"SRVINIT"
        ws.send_bytes(b"hello")
        assert ws.receive_bytes() == b"hello"
        # The fake upstream closes after its echo, so the gateway closes the
        # client session next; observing that close proves the app handler
        # returned before the TestClient session exits (no exit-cancel race).
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()


def test_ws_wrong_password_4001(gateway_server, monkeypatch):
    fake = _FakeRFB("testpass")
    monkeypatch.setattr(vnc, "MINIPC_VNC_WS", f"ws://127.0.0.1:{fake.port}")
    monkeypatch.setattr(vnc, "vnc_password", lambda: "wrong")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect(
            "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
        ) as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 4001


def test_ws_no_password_4003(gateway_server, monkeypatch):
    monkeypatch.setattr(vnc, "vnc_password", lambda: None)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect(
            "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
        ) as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 4003


def test_ws_no_auth_rejected(gateway_server, monkeypatch):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect("/screen/minipc/ws", subprotocols=["binary"]) as ws:
            ws.receive_bytes()
    assert exc_info.value.code in (1008, 403)


def test_ws_upstream_unreachable_4002(gateway_server, monkeypatch):
    monkeypatch.setattr(vnc, "MINIPC_VNC_WS", "ws://127.0.0.1:1")
    monkeypatch.setattr(vnc, "vnc_password", lambda: "testpass")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect(
            "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
        ) as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 4002


# --- (e) M1/M2/L2: resource safety, origin guard, session cap -------------------

class _StallRFB:
    """Upstream that sends its version then stalls forever (handshake timeout)."""

    def __init__(self):
        self.port = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5), "stall RFB server did not start"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def handler(ws):
            await ws.send(b"RFB 003.008\n")
            await asyncio.sleep(3600)

        async def start():
            self._server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]

        loop.run_until_complete(start())
        self._ready.set()
        loop.run_forever()


def _mint_cookie() -> str:
    client = _client()
    resp = client.get("/screen", headers=IOS_AUTH_HEADER)
    cookie = resp.cookies.get("screen_session")
    assert cookie, resp.headers
    return cookie


def test_ws_client_exit_closes_upstream_within_3s(gateway_server, monkeypatch):
    fake = _FakeRFB("testpass")
    monkeypatch.setattr(vnc, "MINIPC_VNC_WS", f"ws://127.0.0.1:{fake.port}")
    monkeypatch.setattr(vnc, "vnc_password", lambda: "testpass")
    with _client().websocket_connect(
        "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
    ) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        # M1: exiting right after the version line must tear down the upstream
        # leg too — the fake server observes its close within 3 s.
    assert fake.closed.wait(3), "gateway left the upstream websocket open"


def test_ws_upstream_handshake_timeout_4002(gateway_server, monkeypatch):
    fake = _StallRFB()
    monkeypatch.setattr(vnc, "MINIPC_VNC_WS", f"ws://127.0.0.1:{fake.port}")
    monkeypatch.setattr(vnc, "vnc_password", lambda: "testpass")
    monkeypatch.setattr(vnc, "HANDSHAKE_TIMEOUT", 0.5)
    start = time.monotonic()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect(
            "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
        ) as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 4002
    assert time.monotonic() - start < 2


def test_ws_cookie_origin_mismatch_1008(gateway_server, monkeypatch):
    monkeypatch.setenv("SCREEN_PUBLIC_BASE", "https://minipc.<tailnet>.<tld>:8790")
    cookie = _mint_cookie()
    headers = {"Cookie": f"screen_session={cookie}", "Origin": "https://evil.<tailnet>.<tld>"}
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect("/screen/minipc/ws", headers=headers) as ws:
            ws.receive_bytes()
    # Pre-accept close: starlette versions surface it as 1008 or as HTTP 403.
    assert exc_info.value.code in (1008, 403), exc_info.value.code


def test_ws_cookie_origin_matching_host_proceeds(gateway_server, monkeypatch):
    fake = _FakeRFB("testpass")
    monkeypatch.setattr(vnc, "MINIPC_VNC_WS", f"ws://127.0.0.1:{fake.port}")
    monkeypatch.setattr(vnc, "vnc_password", lambda: "testpass")
    monkeypatch.setenv("SCREEN_PUBLIC_BASE", "https://minipc.<tailnet>.<tld>:8790")
    cookie = _mint_cookie()
    headers = {"Cookie": f"screen_session={cookie}", "Origin": "http://testserver"}
    with _client().websocket_connect("/screen/minipc/ws", headers=headers) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"  # handshake proceeded


def test_ws_bearer_no_origin_proceeds(gateway_server, monkeypatch):
    fake = _FakeRFB("testpass")
    monkeypatch.setattr(vnc, "MINIPC_VNC_WS", f"ws://127.0.0.1:{fake.port}")
    monkeypatch.setattr(vnc, "vnc_password", lambda: "testpass")
    with _client().websocket_connect(
        "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
    ) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"  # bearer skips the Origin check


def test_ws_session_cap_4004(gateway_server, monkeypatch):
    monkeypatch.setattr(vnc, "MAX_VNC_SESSIONS", 0)
    monkeypatch.setattr(vnc, "vnc_password", lambda: "testpass")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect(
            "/screen/minipc/ws", headers=_WS_HEADERS, subprotocols=["binary"]
        ) as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 4004
