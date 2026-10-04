"""Flight handoff flow: one-tap checkout handoff from a Mac Claude session to the phone.

DESIGN.md "Feature data flows" #2. The requesting Claude session POSTs
/handoff/flight, gets {id, link}, and sends the jarvis://handoff/<id> link to the
owner itself (WhatsApp/iMessage lives in that session, not the gateway). When the
app arrives over WS (role=ios) and the handoff_open message reaches
`handle_handoff_open`, the gateway replies with the checkout URL, pushes a
navigate command to the ios role, and walks the handoff pending -> accepted ->
driving. A handoff read past its TTL flips to expired and notifies the
requesting session exactly once through the injectable `notify` channel
(DESIGN.md "Handoff link ignored" row).
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gateway import registry
from gateway.auth import require_bearer

logger = logging.getLogger(__name__)

router = APIRouter()

LINK_PREFIX = "jarvis://handoff/"
HANDOFF_TTL_SECONDS = 300
IOS_ROLE = "ios"

# Module-level in-memory store, id -> handoff (single-user gateway; DESIGN has no DB).
_handoffs: dict[str, dict] = {}

# Injectable async notify channel for TTL expiry; the default records in-module.
notifications: list[dict] = []


async def _default_notify(event: dict) -> None:
    notifications.append(event)


notify = _default_notify


class FlightHandoffBody(BaseModel):
    url: str
    context: str = ""


def create_handoff(url: str, context: str = "", ttl: float | None = None) -> dict:
    """Store a pending handoff and return it. `ttl` overrides HANDOFF_TTL_SECONDS (test seam)."""
    handoff_id = uuid.uuid4().hex
    _handoffs[handoff_id] = {
        "id": handoff_id,
        "url": url,
        "context": context,
        "state": "pending",
        "history": ["pending"],
        "expires_at": time.monotonic() + (HANDOFF_TTL_SECONDS if ttl is None else ttl),
    }
    return _handoffs[handoff_id]


async def get_handoff(handoff_id: str) -> dict | None:
    """Return the handoff, expiring it (state flip + one notify) when read past its TTL."""
    handoff = _handoffs.get(handoff_id)
    if handoff is None:
        return None
    if handoff["state"] != "expired" and time.monotonic() > handoff["expires_at"]:
        handoff["state"] = "expired"
        handoff["history"].append("expired")
        await notify({"id": handoff_id, "event": "expired"})
        logger.info("flight: handoff %s expired after TTL", handoff_id)
    return handoff


def _set_state(handoff: dict, state: str) -> None:
    handoff["state"] = state
    handoff["history"].append(state)


async def handle_handoff_open(message_id: str | None, handoff_id: str) -> dict | None:
    """Handle the app's {id, type: "handoff_open", payload: {id}} WS message (role=ios).

    Replies {id, ok, data: {"url": ...}}, pushes {id, type: "navigate",
    payload: {"url": ...}} to the ios role via send_to_role, then sets the
    state accepted, then driving — in that order. Returns the reply data, or
    None for an unknown/expired handoff (replied with ok: False).
    """
    handoff = await get_handoff(handoff_id)
    if handoff is None or handoff["state"] == "expired":
        await registry.send_to_role(
            IOS_ROLE, {"id": message_id, "ok": False, "data": {"error": "handoff unknown or expired"}}
        )
        return None
    url = handoff["url"]
    await registry.send_to_role(IOS_ROLE, {"id": message_id, "ok": True, "data": {"url": url}})
    await registry.send_to_role(IOS_ROLE, {"id": message_id, "type": "navigate", "payload": {"url": url}})
    _set_state(handoff, "accepted")
    _set_state(handoff, "driving")
    return {"url": url}


@router.post("/handoff/flight", dependencies=[Depends(require_bearer)])
async def create_flight_handoff(body: FlightHandoffBody) -> dict:
    """Create a pending handoff; the caller sends the returned link to the owner itself."""
    handoff = create_handoff(body.url, body.context)
    return {"id": handoff["id"], "link": LINK_PREFIX + handoff["id"]}
