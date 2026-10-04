"""T1.2 check: WS endpoint + agent registry (pytest).

Runs `gateway.app` via the shared tests/conftest.py `gateway_server` fixture
(in-process uvicorn on port 0, fixture tokens through a name-aware get_token
patch), so this passes regardless of vault state. Uses the installed
`websockets` client to exercise S2 header auth (`Authorization: Bearer`,
role-bound), unknown-role close 4008, /roles listing, the {id, type,
payload} -> {id, ok, data} envelope, handoff_open forbidden for the windows
role, and the replace-on-reconnect rule.
"""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:  # InvalidStatusCode is deprecated in websockets >=14 but raised pre-13
        from websockets.exceptions import InvalidStatusCode
    except ImportError:  # pragma: no cover
        InvalidStatusCode = InvalidStatus


def ws_url(base: str, role: str) -> str:
    return f"ws://{base}/ws?role={role}"


def handshake_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "status_code", None)


def close_code(exc: ConnectionClosed, ws: websockets.ClientConnection) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    return rcvd.code if rcvd is not None else getattr(exc, "code", None) or ws.close_code


async def expect_connect_rejected(url: str, headers: dict | None, expected_status: int) -> None:
    try:
        async with websockets.connect(url, **{_WS_HEADERS_KWARG: headers or {}}):
            raise AssertionError(f"handshake accepted: {url} {headers}")
    except (InvalidStatus, InvalidStatusCode) as exc:
        status = handshake_status(exc)
        assert status == expected_status, f"handshake status {status!r} != {expected_status}"


async def run_all(server_factory) -> None:
    async with server_factory() as base:
        print(f"[1] server up on {base}")
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            # Missing / wrong header, and role-binding: the shared token must not
            # authenticate role ios, the ios token must not authenticate role windows.
            await expect_connect_rejected(ws_url(base, "ios"), None, 403)
            await expect_connect_rejected(ws_url(base, "ios"), {"Authorization": "Bearer wrong"}, 403)
            await expect_connect_rejected(ws_url(base, "ios"), AUTH_HEADER, 403)  # shared token
            await expect_connect_rejected(ws_url(base, "windows"), None, 403)
            await expect_connect_rejected(ws_url(base, "windows"), IOS_AUTH_HEADER, 403)  # ios token
            print("[2] header auth: missing/wrong/cross-role tokens rejected (403)")

            # Unknown role -> accepted then closed with 4008.
            async with websockets.connect(ws_url(base, "router")) as ws:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=10)
                    raise AssertionError("unknown role was not closed")
                except ConnectionClosed as exc:
                    code = close_code(exc, ws)
                    assert code == 4008, f"close code {code!r} != 4008"
            print("[3] unknown role closed with 4008")

            # Connect one socket per role; each rides its own Bearer header.
            windows_ws = await websockets.connect(
                ws_url(base, "windows"), **{_WS_HEADERS_KWARG: AUTH_HEADER}
            )
            ios_ws = await websockets.connect(
                ws_url(base, "ios"), **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
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

            # S2 decision C: handoff_open from the windows role -> forbidden.
            await windows_ws.send(json.dumps(
                {"id": "f1", "type": "handoff_open", "payload": {"id": "whatever"}}
            ))
            reply = json.loads(await asyncio.wait_for(windows_ws.recv(), timeout=10))
            assert reply == {"id": "f1", "ok": False, "data": "forbidden"}, f"reply: {reply}"
            print("[6] handoff_open from windows role -> forbidden reply, socket stays open")

            # Replace: a second windows connection closes the first; registry keeps one.
            windows_ws2 = await websockets.connect(
                ws_url(base, "windows"), **{_WS_HEADERS_KWARG: AUTH_HEADER}
            )
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
            print("[7] reconnect replaced: old socket closed, one windows connection, new one live")

            await windows_ws2.close()
            await ios_ws.close()


def test_ws_registry(gateway_server) -> None:
    asyncio.run(run_all(gateway_server))
