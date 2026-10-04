"""pystray tray icon for the Windows agent.

Runs the agent loop (`windows.agent.main.run`) in a background thread and
reflects its state in the tray: green = online (agent thread alive, gateway
reachable), grey = offline (starting / retrying / stopped). Quit lives in the
menu (DESIGN.md "Components", windows row: tray agent, one .exe).

The gateway address is resolved by the agent itself (JARVIS_GATEWAY_URL env
or the gateway-url file — C-HIGH-4); the probe here uses the same resolution.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from urllib.parse import urlsplit

import pystray
from PIL import Image, ImageDraw

from common import config
from windows.agent import main as agent_main

logger = logging.getLogger(__name__)

PROBE_INTERVAL_S = 5.0
PROBE_TIMEOUT_S = 1.0
ICON_SIZE = 64
ONLINE_COLOR = (46, 204, 64)  # green
OFFLINE_COLOR = (144, 148, 151)  # grey


def _disc(color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((4, 4, ICON_SIZE - 4, ICON_SIZE - 4), fill=color + (255,))
    return img


def _gateway_address() -> tuple[str, int]:
    """Host/port to probe: the configured gateway URL's authority, else the default port."""
    base = agent_main.load_gateway_url()
    if base:
        parts = urlsplit(base if "://" in base else "ws://" + base)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme in ("https", "wss") else 80)
        return host, port
    return "127.0.0.1", config.PORT


def _gateway_reachable() -> bool:
    try:
        with socket.create_connection(_gateway_address(), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _run_agent() -> None:
    """Run the agent loop from windows.agent.main on its own event loop.

    Passes JARVIS_GATEWAY_URL through so an env-configured URL wins even when
    a stale gateway-url file exists; resolution details stay in agent_main.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(agent_main.run(url=os.environ.get("JARVIS_GATEWAY_URL")))
    except SystemExit as exc:
        logger.error("agent exited: %s", exc)
    finally:
        loop.close()


def _watch(icon: pystray.Icon, agent_thread: threading.Thread) -> None:
    """Poll agent liveness + gateway reachability and refresh the tray state."""
    while True:
        online = agent_thread.is_alive() and _gateway_reachable()
        try:
            icon.icon = _disc(ONLINE_COLOR if online else OFFLINE_COLOR)
            icon.title = f"Jarvis agent — {'online' if online else 'offline'}"
            icon.update_icon()
            icon.update_title()
        except Exception:
            # run() may not have set the icon up yet on the first tick; retry.
            logger.debug("tray update before icon ready; retrying", exc_info=True)
        time.sleep(PROBE_INTERVAL_S)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    agent_thread = threading.Thread(target=_run_agent, name="agent", daemon=True)
    agent_thread.start()
    icon = pystray.Icon(
        "jarvis-agent",
        _disc(OFFLINE_COLOR),
        "Jarvis agent — starting",
        pystray.Menu(pystray.MenuItem("Quit", lambda ic: ic.stop())),
    )
    threading.Thread(target=_watch, args=(icon, agent_thread), name="tray-watch", daemon=True).start()
    try:
        icon.run()
    except KeyboardInterrupt:
        logger.info("tray stopped")


if __name__ == "__main__":
    main()
