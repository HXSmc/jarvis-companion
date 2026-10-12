"""Windows agent entrypoint: WebSocket client to the gateway.

Connects to the gateway's `/ws?role=windows` with the shared token in the
`Authorization: Bearer` header (never a query param — those land in access
logs), answers allowlisted commands, and reconnects with exponential
backoff + jitter (DESIGN.md "Gateway down" row).

The gateway address comes from the `JARVIS_GATEWAY_URL` env var, else the
first line of the gateway-url file — `%APPDATA%\\jarvis\\gateway_url` on
Windows, `~/.jarvis/gateway_url` elsewhere. It may be an http(s) base URL
(the gateway's tailscale-serve address), a bare ws(s) URL, or host:port;
`ws://127.0.0.1` is NOT hardcoded (C-HIGH-4: that is the PC's own loopback,
which is never where the Mac gateway lives).

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
import uuid

import psutil
import websockets

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


def gateway_url_file_path() -> "os.PathLike[str]":
    """OS-appropriate gateway-url file: %APPDATA%/jarvis/gateway_url on Windows,
    ~/.jarvis/gateway_url elsewhere."""
    if sys.platform == "win32":
        return os.path.join(os.environ["APPDATA"], "jarvis", "gateway_url")
    return os.path.join(os.path.expanduser("~"), ".jarvis", "gateway_url")


def load_gateway_url() -> str | None:
    """The Mac gateway's address: `JARVIS_GATEWAY_URL` env var, else the url
    file's first line (same resolution order and error handling as load_token)."""
    env_url = os.environ.get("JARVIS_GATEWAY_URL")
    if env_url:
        return env_url.strip()
    try:
        with open(gateway_url_file_path(), "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
    except OSError:
        logger.error(
            "no JARVIS_GATEWAY_URL env var and gateway-url file not readable: %s",
            gateway_url_file_path(),
        )
        return None
    if not first_line:
        logger.error("gateway-url file is empty: %s", gateway_url_file_path())
        return None
    return first_line


def _ws_url(base: str) -> str:
    """Normalize a configured gateway base URL into the role=windows WS URL.

    Accepts https://host[:port]/, http://host:port, ws://host:port, or a bare
    host:port — anything after the authority (a path) is dropped and replaced
    with the gateway's /ws route.
    """
    base = base.strip().rstrip("/")
    if "://" not in base:
        base = "ws://" + base
    scheme, _, rest = base.partition("://")
    authority = rest.split("/", 1)[0]
    if scheme == "https":
        scheme = "wss"
    elif scheme == "http":
        scheme = "ws"
    return f"{scheme}://{authority}/ws?role=windows"


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


def _adapter_mac() -> int | None:
    """First non-virtual interface's AF_LINK MAC as an int (None if none found)."""
    # ponytail: substring list is not exhaustive — an unlisted virtual adapter
    # slips through; upgrade = query the adapter type (SNMP if_type) instead of names.
    virtual = ("Tailscale", "Loopback", "vEthernet", "WSL", "Bluetooth", "VMware", "Virtual")
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        if any(v in name for v in virtual):
            continue
        st = stats.get(name)
        if st is None or not st.isup:
            continue
        for addr in addrs:
            if addr.family == psutil.AF_LINK:
                hex_ = addr.address.replace(":", "").replace("-", "")
                if len(hex_) == 12 and int(hex_[:2], 16) & 0x01 == 0:
                    return int(hex_, 16)
    return None


def _hello_envelope() -> str:
    """Connect-time `hello` (Q2): reports the NIC MAC; payload omits it when random."""
    mac = _adapter_mac()
    if mac is None:
        mac = uuid.getnode()
    payload = {} if (mac >> 40) & 0x01 else {"mac": ":".join(f"{(mac >> s) & 0xFF:02x}" for s in range(40, -1, -8))}
    return json.dumps({"id": f"{random.getrandbits(32):08x}", "type": "hello", "payload": payload})


async def run(url: str | None = None, token: str | None = None) -> None:
    url = load_gateway_url() if url is None else url
    token = load_token() if token is None else token
    if url is None:
        logger.error(
            "no gateway URL available (env JARVIS_GATEWAY_URL or gateway-url file); "
            "agent cannot start"
        )
        raise SystemExit(1)
    if token is None:
        logger.error("no bearer token available (env JARVIS_TOKEN or token file); agent cannot start")
        raise SystemExit(1)
    url = _ws_url(url)
    headers = {"Authorization": f"Bearer {token}"}
    connect_kwargs = (
        {"additional_headers": headers}
        if int(websockets.version.version.split(".")[0]) >= 14
        else {"extra_headers": headers}
    )
    backoff = INITIAL_BACKOFF_S
    while True:
        try:
            logger.info("connecting to gateway at %s", url)
            async with websockets.connect(url, **connect_kwargs) as ws:
                logger.info("connected")
                backoff = INITIAL_BACKOFF_S
                await ws.send(_hello_envelope())
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
