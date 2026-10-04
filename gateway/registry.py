"""Agent registry: one WebSocket connection per role; a newer connection replaces the older one."""

from __future__ import annotations

import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)

ROLES = ("ios", "windows")

# One socket per role. A register for an occupied role closes and evicts the old
# socket (pre-made decision from the T1.2 spec), so the dict never holds two.
_connections: dict[str, WebSocket] = {}


async def register(role: str, ws: WebSocket) -> None:
    """Register `ws` as the connection for `role`, closing any socket it displaces."""
    old = _connections.get(role)
    _connections[role] = ws
    if old is not None and old is not ws:
        logger.info("registry: role %s replaced, closing displaced connection", role)
        try:
            await old.close(code=4009, reason="replaced by a newer connection")
        except Exception:
            logger.info("registry: displaced %s connection already dead", role)


async def unregister(role: str, ws: WebSocket) -> None:
    """Drop `role`'s registration only if `ws` is still the socket holding it.

    A replaced socket's handler unwinds after its successor registered; the
    identity check keeps it from evicting the newer connection.
    """
    if _connections.get(role) is ws:
        del _connections[role]


def roles() -> list[str]:
    """Roles with a live connection right now."""
    return sorted(_connections)


async def send_to_role(role: str, payload: dict) -> bool:
    """Send a JSON payload to `role`'s connection. False when the role is offline."""
    ws = _connections.get(role)
    if ws is None:
        return False
    await ws.send_json(payload)
    return True
