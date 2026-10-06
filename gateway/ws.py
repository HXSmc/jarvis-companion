"""WebSocket endpoint: role-based agent connections with token auth and echo-ack routing.

Mounted by `gateway.app`. Auth is HEADER-ONLY:
`Authorization: Bearer <token>` (MED-2 — the token must never ride the URL
string). Tokens are role-bound — role `ios` must present the vault's
`ios_token:` value, role `windows` the shared `token:` value — so a holder of
one token cannot claim the other role. The authenticated role is remembered by
the connection (and the registry, which keys sockets by it) and gates
message dispatch: `handoff_open` is accepted only from role `ios`.
"""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from common import config
from gateway import agents, registry, wake
from gateway.auth import require_bearer
from gateway.flows import flight, signin

logger = logging.getLogger(__name__)

router = APIRouter()

UNKNOWN_ROLE_CLOSE = 4008
IOS_ROLE = "ios"


async def _handle_handoff_open(message: dict, role: str) -> dict | None:
    """Route a handoff_open envelope into the owning flow, chosen by handoff kind.

    Sign-in handoffs carry credentials, so `handoff_open` is accepted ONLY
    from a connection authenticated as role `ios`; any other role gets an
    ok:False "forbidden" reply on the same socket.

    The handoff record is looked up first; a sign-in handoff (kind="signin",
    set by gateway.flows.signin) goes to signin.handle_handoff_open (which adds
    the credential prefill push on top of flight's shared machine), anything
    else to flight.handle_handoff_open.

    Returns None when the flow replied on the socket itself (it owns both the
    {id, ok, data} reply and the navigate push via send_to_role), or an error
    envelope to send when dispatch itself failed (malformed payload /
    forbidden role). Unknown or expired handoffs are replied with
    ok: False by the flight flow itself; the socket stays open either way.
    """
    if role != IOS_ROLE:
        return {"id": message.get("id"), "ok": False, "data": "forbidden"}
    payload = message.get("payload")
    handoff_id = payload.get("id") if isinstance(payload, dict) else None
    if not handoff_id:
        return {"id": message.get("id"), "ok": False, "data": "missing handoff id"}
    handoff = await flight.get_handoff(handoff_id)
    if handoff is not None and handoff.get("kind") == "signin":
        await signin.handle_handoff_open(message.get("id"), handoff_id)
    else:
        await flight.handle_handoff_open(message.get("id"), handoff_id)
    return None


# type -> handler. A handler gets (message, role) — the connection's
# authenticated role — and returns the envelope to send, or None when it has
# already replied on the socket itself. Types not listed keep the generic ack.
_HANDLERS = {"handoff_open": _handle_handoff_open, "hello": wake.handle_hello}


def _expected_token(role: str) -> str | None:
    """The vault token bound to `role`: ios_token for ios, shared token otherwise."""
    if role == IOS_ROLE:
        return config.get_token(name="ios_token")
    return config.get_token()


def _authorized(websocket: WebSocket, role: str) -> bool:
    """True when the Bearer header matches the token bound to `role`."""
    expected = _expected_token(role)
    authorization = websocket.headers.get("authorization", "")
    provided = authorization[len("Bearer "):] if authorization.startswith("Bearer ") else None
    return bool(
        expected
        and provided
        and hmac.compare_digest(provided.encode(), expected.encode())
    )


@router.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    role: str = Query(default=""),
) -> None:
    if role not in registry.ROLES:
        await websocket.accept()
        await websocket.close(code=UNKNOWN_ROLE_CLOSE, reason="unknown role")
        return
    if not _authorized(websocket, role):
        # Before accept() the close is delivered as a rejected handshake (HTTP 403).
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info("ws: %s connected", role)
    await registry.register(role, websocket)
    try:
        while True:
            # receive() (not receive_text()): the iOS URLSessionWebSocketTask sends
            # BINARY frames, so both text and bytes must decode into envelopes.
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(code=frame.get("code", 1000))
            if isinstance(frame.get("text"), str):
                raw: str | None = frame["text"]
            elif isinstance(frame.get("bytes"), (bytes, bytearray)):
                raw = frame["bytes"].decode("utf-8", errors="replace")
            else:  # e.g. websocket.accept repeats — nothing to parse
                continue
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("envelope is not a JSON object")
            except ValueError:
                await websocket.send_json(
                    {"id": None, "ok": False, "data": {"error": "invalid envelope"}}
                )
                continue
            # A reply to a pending /agents/*/command resolves its future and is
            # consumed — the generic ack would only confuse the agent.
            if agents.resolve_reply(message):
                continue
            handler = _HANDLERS.get(message.get("type"))
            if handler is None:  # unknown type keeps the generic ack
                await websocket.send_json(
                    {"id": message.get("id"), "ok": True, "data": message.get("payload")}
                )
                continue
            try:
                reply = await handler(message, role)
            except Exception:
                logger.exception("ws: %s handler failed", message.get("type"))
                reply = {"id": message.get("id"), "ok": False, "data": "handler error"}
            if reply is not None:  # flow handlers may reply on the socket themselves
                await websocket.send_json(reply)
    except WebSocketDisconnect:
        pass
    finally:
        await registry.unregister(role, websocket)
        logger.info("ws: %s disconnected", role)


@router.get("/roles", dependencies=[Depends(require_bearer)])
async def list_roles() -> dict[str, list[str]]:
    """Roles with a live WS connection right now."""
    return {"roles": registry.roles()}
