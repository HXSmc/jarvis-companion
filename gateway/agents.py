"""Agent command router: HTTP POST -> WS round-trip to the windows agent.

DESIGN.md "Feature data flows" #1: `mac.voice.brain` routes an allowlisted
transcript through `mac.voice.router` and POSTs the matched command here; this
module forwards `router.request_envelope(cmd)` to the windows agent's WS
connection via `registry.send_to_role` and awaits the matching `{id, ok, data}`
reply on a pending-futures map keyed by the envelope id — `gateway.ws` resolves
incoming replies into those futures (agents.resolve_reply) instead of echoing
the generic ack. No windows connection -> 503; no reply within the timeout ->
504 (both JSON details). Claude sessions can hit this endpoint directly too.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from gateway import registry
from gateway.auth import require_bearer
from mac.voice import router as voice_router

logger = logging.getLogger(__name__)

router = APIRouter()

WINDOWS_ROLE = "windows"
NOT_CONNECTED_DETAIL = "windows agent not connected"
TIMEOUT_DETAIL = "windows agent did not reply within the timeout"

# How long the POST waits for the agent's WS reply before giving up (504).
REPLY_TIMEOUT_S = 10.0

# envelope id -> Future resolved with the agent's {id, ok, data} reply.
_pending_replies: dict[str, asyncio.Future] = {}


def resolve_reply(envelope: dict) -> bool:
    """Resolve a pending command's reply envelope; True when consumed here.

    Called by gateway.ws for every parsed envelope before the handler lookup:
    one carrying `ok` whose id matches a pending POST completes that request's
    future (the POST returns it); anything else returns False and keeps the
    normal dispatch (generic ack / flow handler).
    """
    future = _pending_replies.get(envelope.get("id")) if "ok" in envelope else None
    if future is None or future.done():
        return False
    future.set_result(envelope)
    return True


class CommandBody(BaseModel):
    cmd: str


async def agent_roundtrip(envelope: dict) -> dict:
    """Send one command envelope to the windows agent and await its reply.

    No connection -> 503; no reply within REPLY_TIMEOUT_S -> 504. Shared by the
    /agents POST (voice-routed commands) and gateway.screen's frame/input/open
    round-trips — SC2 spec extraction.
    """
    if WINDOWS_ROLE not in registry.roles():
        raise HTTPException(status_code=503, detail=NOT_CONNECTED_DETAIL)
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_replies[envelope["id"]] = future
    try:
        if not await registry.send_to_role(WINDOWS_ROLE, envelope):
            raise HTTPException(status_code=503, detail=NOT_CONNECTED_DETAIL)
        logger.info("agents: sent %r to %s as %s", envelope["payload"].get("cmd"), WINDOWS_ROLE, envelope["id"])
        try:
            reply = await asyncio.wait_for(future, timeout=REPLY_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("agents: no reply for %s within %.1fs", envelope["id"], REPLY_TIMEOUT_S)
            raise HTTPException(status_code=504, detail=TIMEOUT_DETAIL)
    finally:
        _pending_replies.pop(envelope["id"], None)
    return reply


@router.post("/agents/windows/command", dependencies=[Depends(require_bearer)])
async def windows_command(body: CommandBody) -> dict:
    """Forward one allowlisted command to the windows agent and await its reply."""
    reply = await agent_roundtrip(voice_router.request_envelope(body.cmd))
    return {"ok": bool(reply.get("ok")), "data": reply.get("data")}
