"""Windows agent entrypoint: WebSocket client to the gateway.

Connects to `ws://127.0.0.1:<PORT>/ws?role=windows&token=<...>`, answers
allowlisted commands, and reconnects with exponential backoff + jitter
(DESIGN.md "Gateway down" row).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random

import websockets

from common import config
from windows.agent import commands

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0


def handle_message(raw: str) -> dict | None:
    """Turn an incoming `{id, type, payload}` envelope into a `{id, ok, data}` reply.

    Returns None when there is nothing to reply to (non-command or non-JSON).
    Payload may be the bare command name or `{cmd: ..., args: ...}`.
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("dropping non-JSON message")
        return None
    if not isinstance(msg, dict) or msg.get("type") != "command":
        return None
    payload = msg.get("payload")
    if isinstance(payload, dict):
        name, args = payload.get("cmd"), payload.get("args")
    else:
        name, args = payload, None
    ok, data = commands.run_command(name, args)
    return {"id": msg.get("id"), "ok": ok, "data": data}


async def run(host: str = "127.0.0.1", port: int | None = None, token: str | None = None) -> None:
    port = config.PORT if port is None else port
    token = config.get_token() if token is None else token
    if token is None:
        logger.error("no bearer token available from the vault; agent cannot start")
        raise SystemExit(1)
    url = f"ws://{host}:{port}/ws?role=windows&token={token}"
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            logger.info("connecting to gateway at %s:%s", host, port)
            async with websockets.connect(url) as ws:
                logger.info("connected")
                backoff = INITIAL_BACKOFF_S
                async for raw in ws:
                    reply = handle_message(raw)
                    if reply is not None:
                        await ws.send(json.dumps(reply))
        except (OSError, websockets.WebSocketException) as exc:
            sleep_for = backoff * (0.5 + random.random())  # jitter
            logger.warning("gateway unreachable (%s); reconnect in %.1fs", exc, sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("agent stopped")


if __name__ == "__main__":
    main()
