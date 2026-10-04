"""Flight handoff flow: one-tap checkout handoff from a Mac Claude session to the phone.

DESIGN.md "Feature data flows" #2. The requesting Claude session POSTs
/handoff/flight, gets {id, link}, and sends the jarvis://handoff/<id> link to the
owner itself (WhatsApp/iMessage lives in that session, not the gateway). When the
app arrives over WS (role=ios) and the handoff_open message reaches
`handle_handoff_open`, the gateway replies with the checkout URL, pushes a
navigate command to the ios role, and walks the handoff pending -> accepted ->
driving. A handoff read past its TTL flips to expired and notifies the
requesting session exactly once through the injectable `notify` channel
(DESIGN.md "Handoff link ignored" row) — and not only when read: a background
sweeper task (started on first handoff creation) expires untouched handoffs so
an ignored link still notifies. `GET /handoff/{id}` lets the requesting session
poll the state (`{id, state, kind}`).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from gateway import registry
from gateway.auth import require_bearer

logger = logging.getLogger(__name__)

router = APIRouter()

LINK_PREFIX = "jarvis://handoff/"
HANDOFF_TTL_SECONDS = 300
SWEEP_INTERVAL_S = 1.0
IOS_ROLE = "ios"

# Module-level in-memory store, id -> handoff (single-user gateway; DESIGN has no DB).
_handoffs: dict[str, dict] = {}

# Injectable async notify channel for TTL expiry; the default records in-module.
notifications: list[dict] = []

# The sweeper task and the loop it runs on (tasks are loop-bound; a new event
# loop — e.g. the next test's asyncio.run — needs its own sweeper).
_sweeper_task: asyncio.Task | None = None
_sweeper_loop: asyncio.AbstractEventLoop | None = None


async def _default_notify(event: dict) -> None:
    notifications.append(event)


notify = _default_notify


async def _expire(handoff: dict) -> None:
    """Flip a handoff to expired and notify once (guarded by the state check)."""
    handoff["state"] = "expired"
    handoff["history"].append("expired")
    await notify({"id": handoff["id"], "event": "expired"})
    logger.info("flight: handoff %s expired after TTL", handoff["id"])


async def _sweep_expired() -> None:
    """Periodically expire handoffs nobody opened, so an ignored link still notifies.

    The read-path expiry in get_handoff only fires when someone looks the
    handoff up; this loop covers the handoff the owner never taps (C-MED-2).
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        now = time.monotonic()
        # Snapshot: notify() awaits, and a create during that await must not
        # mutate the dict under iteration.
        for handoff in list(_handoffs.values()):
            if handoff["state"] != "expired" and now > handoff["expires_at"]:
                await _expire(handoff)


def ensure_sweeper() -> None:
    """Start the expiry sweeper on the running loop, once per loop."""
    global _sweeper_task, _sweeper_loop
    loop = asyncio.get_running_loop()
    if _sweeper_task is not None and not _sweeper_task.done() and _sweeper_loop is loop:
        return
    _sweeper_loop = loop
    _sweeper_task = loop.create_task(_sweep_expired())


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
        "kind": "flight",  # gateway.ws dispatches handoff_open on this marker (signin overrides)
        "history": ["pending"],
        "expires_at": time.monotonic() + (HANDOFF_TTL_SECONDS if ttl is None else ttl),
    }
    try:  # no running loop (e.g. a sync caller) — the next creation starts it
        ensure_sweeper()
    except RuntimeError:
        pass
    return _handoffs[handoff_id]


async def get_handoff(handoff_id: str) -> dict | None:
    """Return the handoff, expiring it (state flip + one notify) when read past its TTL."""
    handoff = _handoffs.get(handoff_id)
    if handoff is None:
        return None
    if handoff["state"] != "expired" and time.monotonic() > handoff["expires_at"]:
        await _expire(handoff)
    return handoff


def _set_state(handoff: dict, state: str) -> None:
    handoff["state"] = state
    handoff["history"].append(state)


async def handle_handoff_open(message_id: str | None, handoff_id: str) -> dict | None:
    """Handle the app's {id, type: "handoff_open", payload: {id}} WS message (role=ios).

    Single-use (MED-1): only a `pending` handoff can be opened. It replies
    {id, ok, data: {"url": ...}}, pushes {id, type: "navigate",
    payload: {"url": ...}} to the ios role via send_to_role, then sets the
    state accepted, then driving — in that order. A handoff already opened
    (accepted/driving) replies ok: False "handoff already used" with no
    navigate push; unknown/expired keep their existing ok: False reply.
    Returns the reply data, or None for any failure (replied on the socket).
    """
    handoff = await get_handoff(handoff_id)
    if handoff is None or handoff["state"] == "expired":
        await registry.send_to_role(
            IOS_ROLE, {"id": message_id, "ok": False, "data": {"error": "handoff unknown or expired"}}
        )
        return None
    if handoff["state"] != "pending":
        await registry.send_to_role(
            IOS_ROLE, {"id": message_id, "ok": False, "data": "handoff already used"}
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


@router.get("/handoff/{handoff_id}", dependencies=[Depends(require_bearer)])
async def read_handoff(handoff_id: str) -> dict:
    """Poll a handoff's state ({id, state, kind}); 404 for an unknown id.

    Also expires a past-TTL handoff on read, so a poll right after the TTL
    flips it (the sweeper covers the never-polled case).
    """
    handoff = await get_handoff(handoff_id)
    if handoff is None:
        raise HTTPException(status_code=404, detail="handoff not found")
    return {"id": handoff["id"], "state": handoff["state"], "kind": handoff["kind"]}
