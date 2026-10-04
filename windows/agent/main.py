"""Windows agent entrypoint: WebSocket client to the gateway.

Connects to `ws://127.0.0.1:<PORT>/ws?role=windows` with the shared token in
the `Authorization: Bearer` header (never a query param — those land in
access logs), answers allowlisted commands, and reconnects with exponential
backoff + jitter (DESIGN.md "Gateway down" row).

One-time setup: copy the shared `token:` value from the vault's
`## Jarvis Companion` section (WITHOUT the `token: ` prefix) into the token
file — `%APPDATA%\\jarvis\\token` on Windows, `~/.jarvis/token` elsewhere
(first line only) — or export it as the `JARVIS_TOKEN` environment variable.
The agent never reads the vault itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys

import websockets

from common import config
from windows.agent import commands

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0


def token_file_path() -> "os.PathLike[str]":
    """OS-appropriate token file: %APPDATA%/jarvis/token on Windows, ~/.jarvis/token elsewhere."""
    if sys.platform == "win32":
        return os.path.join(os.environ["APPDATA"], "jarvis", "token")
    return os.path.join(os.path.expanduser("~"), ".jarvis", "token")


def load_token() -> str | None:
    """The shared gateway token: `JARVIS_TOKEN` env var, else the token file's first line.

    Never reads the Secrets vault (MED-3): the owner copies the token into the
    file (or env) once at setup. The value is never logged.
    """
    env_token = os.environ.get("JARVIS_TOKEN")
    if env_token:
        return env_token.strip()
    try:
        with open(token_file_path(), "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
    except OSError:
        logger.error(
            "no JARVIS_TOKEN env var and token file not readable: %s", token_file_path()
        )
        return None
    if not first_line:
        logger.error("token file is empty: %s", token_file_path())
        return None
    return first_line


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
    token = load_token() if token is None else token
    if token is None:
        logger.error("no bearer token available (env JARVIS_TOKEN or token file); agent cannot start")
        raise SystemExit(1)
    url = f"ws://{host}:{port}/ws?role=windows"
    headers = {"Authorization": f"Bearer {token}"}
    connect_kwargs = (
        {"additional_headers": headers}
        if int(websockets.version.version.split(".")[0]) >= 14
        else {"extra_headers": headers}
    )
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            logger.info("connecting to gateway at %s:%s", host, port)
            async with websockets.connect(url, **connect_kwargs) as ws:
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
