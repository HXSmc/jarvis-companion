"""Standalone T1.2 check: WS endpoint + agent registry. Exit 0 = pass.

Starts `gateway.app` in-process under uvicorn (background thread, port 0) with
`common.config.get_token` monkeypatched to a fixture token — same approach as
tests/test_gateway_auth.py, so this passes regardless of vault state. Uses the
installed `websockets` client to exercise auth rejection, unknown-role close 4008,
/roles listing, the {id, type, payload} -> {id, ok, data} envelope, and the
replace-on-reconnect rule.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
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

TOKEN = "jarvis_test_token_123"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
# websockets 14.0 renamed connect()'s extra_headers to additional_headers.
_WS_HEADERS_KWARG = (
    "additional_headers"
    if int(websockets.version.version.split(".")[0]) >= 14
    else "extra_headers"
)


def ws_url(base: str, role: str, token: str | None = TOKEN) -> str:
    query = f"role={role}" + (f"&token={token}" if token is not None else "")
    return f"ws://{base}/ws?{query}"


def handshake_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "status_code", None)


def close_code(exc: ConnectionClosed, ws: websockets.ClientConnection) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    return rcvd.code if rcvd is not None else getattr(exc, "code", None) or ws.close_code


async def expect_connect_rejected(url: str, expected_status: int) -> None:
    try:
        async with websockets.connect(url):
            raise AssertionError(f"handshake accepted: {url}")
    except (InvalidStatus, InvalidStatusCode) as exc:
        status = handshake_status(exc)
        assert status == expected_status, f"handshake status {status!r} != {expected_status}"


async def run_all() -> None:
    from gateway.app import app

    config.get_token = lambda path=None: TOKEN  # monkeypatch, per spec

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"127.0.0.1:{port}"
    print(f"[1] server up on {base}")

    async with httpx.AsyncClient(base_url=f"http://{base}") as http:
        # Bad/missing token -> rejected handshake.
        await expect_connect_rejected(ws_url(base, "ios", token=None), 403)
        await expect_connect_rejected(ws_url(base, "ios", token="wrong"), 403)
        print("[2] missing/wrong token rejected (403)")

        # Unknown role -> accepted then closed with 4008.
        async with websockets.connect(ws_url(base, "router")) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=10)
                raise AssertionError("unknown role was not closed")
            except ConnectionClosed as exc:
                code = close_code(exc, ws)
                assert code == 4008, f"close code {code!r} != 4008"
        print("[3] unknown role closed with 4008")

        # Connect one socket per role; ios rides the Bearer header (no query token).
        windows_ws = await websockets.connect(ws_url(base, "windows"))
        ios_ws = await websockets.connect(
            ws_url(base, "ios", token=None), **{_WS_HEADERS_KWARG: AUTH_HEADER}
        )

        roles_no_auth = await http.get("/roles")
        assert roles_no_auth.status_code == 401, "/roles must require the bearer token"
        roles = (await http.get("/roles", headers=AUTH_HEADER)).json()
        assert roles == {"roles": ["ios", "windows"]}, f"/roles: {roles}"
        print(f"[4] GET /roles -> {roles} (401 without token)")

        # Envelope: {id, type, payload} -> {id, ok, data}.
        for ws, msg_id in ((windows_ws, "m1"), (ios_ws, "m2")):
            await ws.send(json.dumps({"id": msg_id, "type": "ping", "payload": {"cmd": "specs"}}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert set(reply) == {"id", "ok", "data"}, f"reply shape: {reply}"
            assert reply["id"] == msg_id and reply["ok"] is True, f"reply: {reply}"
            assert reply["data"] == {"cmd": "specs"}, f"reply: {reply}"
        print("[5] envelope routed both ways: {id, ok, data} acks received")

        # Replace: a second windows connection closes the first; registry keeps one.
        windows_ws2 = await websockets.connect(ws_url(base, "windows"))
        try:
            await asyncio.wait_for(windows_ws.recv(), timeout=10)
            raise AssertionError("old windows socket was not closed after replacement")
        except ConnectionClosed:
            pass
        roles = (await http.get("/roles", headers=AUTH_HEADER)).json()
        assert roles == {"roles": ["ios", "windows"]}, f"/roles after replace: {roles}"
        await windows_ws2.send(json.dumps({"id": "m3", "type": "ping", "payload": {}}))
        reply = json.loads(await asyncio.wait_for(windows_ws2.recv(), timeout=10))
        assert reply == {"id": "m3", "ok": True, "data": {}}, f"reply: {reply}"
        print("[6] reconnect replaced: old socket closed, one windows connection, new one live")

        await windows_ws2.close()
        await ios_ws.close()

    server.should_exit = True


def main() -> None:
    asyncio.run(run_all())
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
