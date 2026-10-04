"""WebSocket endpoint: role-based agent connections with token auth and echo-ack routing.

Mounted automatically by `gateway.app`'s router seam. Auth mirrors `gateway.auth`:
the vault token is read via `common.config.get_token()` at connect time (fail-closed,
monkeypatchable in tests), accepted from the `token` query param (WS clients can't
always set headers) or the Bearer header.
"""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from common import config
from gateway import registry
from gateway.auth import require_bearer

logger = logging.getLogger(__name__)

router = APIRouter()

UNKNOWN_ROLE_CLOSE = 4008


def _authorized(websocket: WebSocket, token: str | None) -> bool:
    """True when the query-param or Bearer token matches the vault token."""
    expected = config.get_token()
    provided = token
    if provided is None:
        authorization = websocket.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):]
    return bool(
        expected
        and provided
        and hmac.compare_digest(provided, expected)
    )


@router.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    role: str = Query(default=""),
    token: str | None = Query(default=None),
) -> None:
    if not _authorized(websocket, token):
        # Before accept() the close is delivered as a rejected handshake (HTTP 403).
        await websocket.close(code=1008)
        return
    if role not in registry.ROLES:
        await websocket.accept()
        await websocket.close(code=UNKNOWN_ROLE_CLOSE, reason="unknown role")
        return

    await websocket.accept()
    logger.info("ws: %s connected", role)
    await registry.register(role, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("envelope is not a JSON object")
            except ValueError:
                await websocket.send_json(
                    {"id": None, "ok": False, "data": {"error": "invalid envelope"}}
                )
                continue
            await websocket.send_json(
                {"id": message.get("id"), "ok": True, "data": message.get("payload")}
            )
    except WebSocketDisconnect:
        pass
    finally:
        await registry.unregister(role, websocket)
        logger.info("ws: %s disconnected", role)


@router.get("/roles", dependencies=[Depends(require_bearer)])
async def list_roles() -> dict[str, list[str]]:
    """Roles with a live WS connection right now."""
    return {"roles": registry.roles()}
